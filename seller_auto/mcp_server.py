"""供 Cursor 等客户端直接调用的 MCP 服务。

这个进程不调用任何模型 API，也不读取 API Key。
关键词和主图是否同款，由客户端当前选中的模型判断。
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import Image, MCPServer
from pydantic import BaseModel, Field

from seller_auto.service import SellerAutoService, ToolPayload

INSTRUCTIONS = """
你正在使用 seller-auto。它只用本地浏览器打开亚马逊墨西哥店铺和美客多阿根廷，
不会自己调用模型，也没有 API Key。看图、写关键词、判断同款都由你完成。

用户会给出店铺 id。如果同时给出一个商品 ASIN 或链接，只调用一次 open_store(seller_id, product)。
这一次就会返回该商品的标题、品牌和主图，接着 set_keywords。不要先打开整店，也不要再调用 next_amazon_product。
没有商品时，open_store(seller_id) 打开整店，然后再调用 next_amazon_product 逐条读取。

检索目标元素和对比主图时使用 grok 4.6 low fast。子代理模型写 cursor-grok-4.6-low-fast，不要只写 grok-4.6。若当前对话不是这个模型，把这两步交给指定该模型的子代理，不要换更慢的模型逐张看。提交判断时 model_name 填 cursor-grok-4.6-low-fast。

用户每次提出新的店铺或商品，都是一轮新任务。直接 open_store，不要先查 session_status，也不要 resume_session。
只有用户明确说「继续上次」时，才调用 resume_session。

调用顺序：
1. open_store(seller_id, product)。每次都会新开一轮。有 ASIN 时 product 必填，返回 amazon_product 后直接 set_keywords。product 留空才遍历店铺。
2. next_amazon_product 只在没有指定商品时使用。每次只打开一条。status=excluded 是服装或食品，继续下一条。
   status=amazon_product 时，你会看到主图、标题和品牌。
3. 根据主图给出适合美客多阿根廷的西班牙语搜索词，调用 set_keywords。
   去掉品牌、尺寸、重量、数量、颜色和空泛用途。品名里的 “para + 物品” 要保留。
4. next_listing_batch 会给出亚马逊参照图和当前这一批美客多列表图。只看前两页。一次提交这一批里的全部候选。
5. submit_listing_matches。每条都要带 candidate_id、similar、confidence、reason 和 model_name。不要提前按相似度截断。卡片上没写 vendido 的，仍要打开详情，低销量只写在详情页。
6. 如果返回里已经带了下一批列表图或广告图，直接判断并提交，不要再调用 next_listing_batch 或 next_ad_batch。广告位只看三个轮播，并且只进一层。
7. 筛选完成后，top_links 只是当前这一条亚马逊商品的排名。
8. 单品模式下，这条商品筛完后 next_step 是 export_results，不要再翻店铺。遍历模式下，只要 next_step 是 next_amazon_product，才打开下一条。
长时间工具还在跑时不要另开一轮。新任务不要 resume_session。

比对标准：
- 颜色不同仍然可以算同一商品。
- 外形、结构和整体构图必须高度相似。
- 只是同一大类、外形明显不同的，similar 必须为 false。
- confidence 取 0 到 1。先完成相似、销量和一层广告位筛选，再按相似度取前 10 条。不低于 0.8 才进入筛选。

这些工具会故意等待十几秒到几十秒，这是风控间隔，不要因为变慢而重复调用同一步。
美客多的搜索、翻页、打开详情和打开广告详情都比点击亚马逊更慢。
如果返回「需要手动验证」，服务会临时打开前台窗口。请让用户完成后，再调用同一个工具。
浏览器最多只有两个标签。不要要求服务并行打开商品。
""".strip()


class MatchDecision(BaseModel):
    candidate_id: str = Field(description="工具返回的候选编号")
    similar: bool = Field(description="整体外形和构图是否高度相似。颜色不同也可以为 true")
    confidence: float = Field(default=0, description="0 到 1 的置信度")
    reason: str = Field(default="", description="简短理由")


def build_server(service: SellerAutoService | None = None) -> MCPServer:
    def invoke(method: str, **kwargs: Any) -> ToolPayload:
        if service is not None:
            return getattr(service, method)(**kwargs)
        from seller_auto.ipc import call

        return call(method, kwargs)

    server = MCPServer("seller-auto", instructions=INSTRUCTIONS)

    @server.tool(structured_output=False)
    def open_store(seller_id: str, product: str = "") -> list[Any]:
        """新开一轮。有 ASIN 时必须这次传入 product，返回里已有主图，不要再调用 next_amazon_product。没有 ASIN 才遍历店铺。"""

        return _blocks(invoke("open_store", seller_id=seller_id, product=product))

    @server.tool(structured_output=False)
    def next_amazon_product() -> list[Any]:
        """只在没有指定商品时使用：慢速点击店铺列表的下一条标题。单品已由 open_store 返回主图。"""

        return _blocks(invoke("next_amazon_product"))

    @server.tool(structured_output=False)
    def set_keywords(product_id: str, keywords: str) -> list[Any]:
        """保存西班牙语关键词，并在美客多风控间隔之后打开搜索页。"""

        return _blocks(invoke("set_keywords", product_id=product_id, keywords=keywords))

    @server.tool(structured_output=False)
    def next_listing_batch() -> list[Any]:
        """返回搜索结果前两页里的下一批列表主图，每批最多 16 张。翻到第 2 页前会等待美客多间隔。对比这些图时使用 cursor-grok-4.6-low-fast。"""

        return _blocks(invoke("next_listing_batch"))

    @server.tool(structured_output=False)
    def submit_listing_matches(matches: list[MatchDecision], model_name: str = "") -> list[Any]:
        """打开所有达到标准的相似商品并检查销量。相似度全部记录，排名在筛选完成之后。"""

        dumped = [item.model_dump() if hasattr(item, "model_dump") else item for item in matches]
        return _blocks(invoke("submit_listing_matches", matches=dumped, model_name=model_name))

    @server.tool(structured_output=False)
    def next_ad_batch(product_id: str) -> list[Any]:
        """慢速收集三个指定轮播里的主图：两块 Productos relacionados，一块 Quienes vieron este producto también compraron。"""

        return _blocks(invoke("next_ad_batch", product_id=product_id))

    @server.tool(structured_output=False)
    def submit_ad_matches(matches: list[MatchDecision], model_name: str = "") -> list[Any]:
        """打开相似的广告商品并只检查销量。这些详情不会再进入下一层广告位。"""

        dumped = [item.model_dump() if hasattr(item, "model_dump") else item for item in matches]
        return _blocks(invoke("submit_ad_matches", matches=dumped, model_name=model_name))

    @server.tool(structured_output=False)
    def session_status() -> list[Any]:
        """查看这一轮的进度。新任务开始时不要调用。"""

        return _blocks(invoke("session_status"))

    @server.tool(structured_output=False)
    def resume_session(session_id: str) -> list[Any]:
        """仅当用户明确说继续上次时，用 session_id 恢复。新任务不要调用。"""

        return _blocks(invoke("resume_session", session_id=session_id))

    @server.tool(structured_output=False)
    def export_results() -> list[Any]:
        """把当前结果写成带时间戳的 JSON，并返回文件路径。"""

        return _blocks(invoke("export_results"))

    return server


def _blocks(payload: ToolPayload) -> list[Any]:
    blocks: list[Any] = [json.dumps(payload.data, ensure_ascii=False)]
    for caption, data in payload.images:
        blocks.append(caption)
        blocks.append(Image(data=data, format="jpeg"))
    return blocks


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
