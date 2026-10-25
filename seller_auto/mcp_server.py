"""供 Cursor 等客户端直接调用的 MCP 服务。

这个进程不调用任何模型 API，也不读取 API Key。
关键词和主图是否同款，由客户端当前选中的模型判断。
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import Image, MCPServer
from pydantic import BaseModel, Field

from seller_auto.pacing import DelaySettings
from seller_auto.service import SellerAutoService, ToolPayload

INSTRUCTIONS = """
你正在使用 seller-auto。它只用本地浏览器打开亚马逊墨西哥店铺和美客多阿根廷，
不会自己调用模型，也没有 API Key。看图、写关键词、判断同款都由你完成。

调用顺序：
1. open_store(店铺链接)。链接形如 https://www.amazon.com.mx/s?me=卖家ID&marketplaceID=卖家ID
2. next_amazon_product。每次只打开一条。status=excluded 是服装或食品，继续下一条。
   status=amazon_product 时，你会看到主图、标题和品牌。
3. 根据主图给出适合美客多阿根廷的西班牙语搜索词，调用 set_keywords。
   去掉品牌、尺寸、重量、数量、颜色和空泛用途。品名里的 “para + 物品” 要保留。
4. next_listing_batch 会给出亚马逊参照图和最多 8 张美客多列表图。只看前两页。
5. submit_listing_matches。每条包含 candidate_id、similar、confidence、reason，并带上 model_name。
6. 对已记录且有销量的详情调用 next_ad_batch(product_id)。参照图是该详情主图，其余是广告位主图。
7. submit_ad_matches。这些详情只判断销量，不要再读它们自己的广告位。
8. 当前商品处理完后回到 next_amazon_product。最后 export_results。
中断后用 session_status 查看进度，或 resume_session(session_id) 恢复。

比对标准：
- 颜色不同仍然可以算同一商品。
- 外形、结构和整体构图必须高度相似。
- 只是同一大类、外形明显不同的，similar 必须为 false。
- confidence 取 0 到 1。服务端默认只打开 similar 且 confidence >= 0.8 的链接。

这些工具会故意等待十几秒到几十秒，这是风控间隔，不要因为变慢而重复调用同一步。
美客多的搜索、翻页、打开详情和打开广告详情都比点击亚马逊更慢。
如果返回「需要手动验证」，请让用户在弹出的浏览器里完成后，再调用同一个工具。
浏览器最多只有两个标签。不要要求服务并行打开商品。
""".strip()


class MatchDecision(BaseModel):
    candidate_id: str = Field(description="工具返回的候选编号")
    similar: bool = Field(description="整体外形和构图是否高度相似。颜色不同也可以为 true")
    confidence: float = Field(default=0, description="0 到 1 的置信度")
    reason: str = Field(default="", description="简短理由")


def build_server(service: SellerAutoService | None = None) -> MCPServer:
    app = service or SellerAutoService(DelaySettings())
    server = MCPServer("seller-auto", instructions=INSTRUCTIONS)

    @server.tool(structured_output=False)
    def open_store(url: str) -> list[Any]:
        """打开亚马逊墨西哥店铺链接，或恢复该卖家已有进度。会先等待一个亚马逊点击间隔。"""

        return _blocks(app.open_store(url))

    @server.tool(structured_output=False)
    def next_amazon_product() -> list[Any]:
        """慢速点击下一条 data-cy=title-recipe 标题。服装和食品会跳过；需要的商品返回标题、品牌和主图。"""

        return _blocks(app.next_amazon_product())

    @server.tool(structured_output=False)
    def set_keywords(product_id: str, keywords: str) -> list[Any]:
        """保存西班牙语关键词，并在美客多风控间隔之后打开搜索页。"""

        return _blocks(app.set_keywords(product_id, keywords))

    @server.tool(structured_output=False)
    def next_listing_batch() -> list[Any]:
        """返回搜索结果前两页里的下一批列表主图，每批最多 8 张。翻到第 2 页前会等待美客多间隔。"""

        return _blocks(app.next_listing_batch())

    @server.tool(structured_output=False)
    def submit_listing_matches(matches: list[MatchDecision], model_name: str = "") -> list[Any]:
        """打开你判定为同款的列表商品。每条详情打开前都等待美客多间隔。没有 vendido 的不记录。"""

        return _blocks(app.submit_listing_matches(matches, model_name))

    @server.tool(structured_output=False)
    def next_ad_batch(product_id: str) -> list[Any]:
        """慢速收集有销量详情里的两个广告位主图，并附上该详情主图作为参照。"""

        return _blocks(app.next_ad_batch(product_id))

    @server.tool(structured_output=False)
    def submit_ad_matches(matches: list[MatchDecision], model_name: str = "") -> list[Any]:
        """打开相似的广告商品并只检查销量。这些详情不会再进入下一层广告位。"""

        return _blocks(app.submit_ad_matches(matches, model_name))

    @server.tool(structured_output=False)
    def session_status() -> list[Any]:
        """查看当前卖家的抓取进度。"""

        return _blocks(app.session_status())

    @server.tool(structured_output=False)
    def resume_session(session_id: str) -> list[Any]:
        """从 output/<seller_id>/state.json 恢复会话。"""

        return _blocks(app.resume_session(session_id))

    @server.tool(structured_output=False)
    def export_results() -> list[Any]:
        """把当前结果写成带时间戳的 JSON，并返回文件路径。"""

        return _blocks(app.export_results())

    return server


def _blocks(payload: ToolPayload) -> list[Any]:
    blocks: list[Any] = [json.dumps(payload.data, ensure_ascii=False)]
    for caption, data in payload.images:
        blocks.append(caption)
        blocks.append(Image(data=data, format="jpeg"))
    return blocks


def main() -> None:
    settings = DelaySettings.from_argv()
    service = SellerAutoService(settings)
    build_server(service).run(transport="stdio")


if __name__ == "__main__":
    main()
