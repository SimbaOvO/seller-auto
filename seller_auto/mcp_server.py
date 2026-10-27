"""供 Cursor 等客户端直接调用的 MCP 服务。

这个进程不调用任何模型 API，也不读取 API Key。
关键词和主图是否同款，由客户端当前选中的模型判断。
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from mcp.server.mcpserver import Image, MCPServer
from pydantic import BaseModel, Field

from seller_auto.service import SellerAutoService, ToolPayload

INSTRUCTIONS = """
你正在使用 seller-auto。它只用本地浏览器打开亚马逊墨西哥店铺和美客多阿根廷。
看图、写关键词、判断同款由你完成。不要搜索项目代码来决定怎么调用工具。

先看用户这句话里有没有指定商品：
- 有 ASIN、商品链接，或「只处理这个商品」：只调用一次 open_product(seller_id, product)。这次返回主图后直接 set_keywords。禁止 open_store，禁止 next_amazon_product，禁止 session_status。
- 只有店铺 id：调用 open_store(seller_id)，然后 next_amazon_product 逐条读取。

每次新任务都新开一轮，不要恢复旧会话。只有用户明确说「继续上次」才调用 resume_session。

检索元素使用 cursor-grok-4.5-low-fast。拿到列表批或广告批后，先对照亚马逊标题看候选标题。服务端已经按标题跳过的不必再看。剩下的里，标题已经能明显看出不是同一商品的，直接记 similar=false，不要进图片对比。标题对得上的，先看本批已经返回的图：包装、折叠、带脚整机、生活场景、只拍局部、装套演示等构图已经明显不是同款的，主代理自己记 similar=false，不要交给看图子代理。只有外形对得上或拿不准的，再按每 4 张切一组看图。没有拿不准的就直接提交，不要为了凑组开子代理。同一轮并行开这些子代理，模型都是 cursor-grok-4.5-low-fast。不要写成方括号参数，也不要换成 grok-4.7-low-fast、grok-4.7-high-fast、cursor-grok-4.6-low-fast 或 cursor-grok-4.6-high-fast。每个只返回自己那组的 JSON，不调用工具。全部返回后把标题过滤、自己看过的图和子代理结果合并成一次提交，model_name 填 cursor-grok-4.5-low-fast。不要串行看完整批，也不要按组分别提交。

每个 ASIN 的顺序：set_keywords 会直接返回第一批列表图 → submit_listing_matches → 广告图由提交的返回带上。整店里服务会自己写表，再在同一次返回里打开下一条。
打开商品后立刻 set_keywords 去美客多比对。卖家精灵查包装，并在该行下方点击「1688找货源」读取货源均价，由后台另一条浏览器并行完成，不要等它们结束。看列表图或广告图时，服务会在同一条美客多浏览器上提前抓下一页，风控间隔不变。列表和广告都结束后服务自己写表，不要调用 export_results。写表前会等卖家精灵和货源结束，再用整理好的美客多链接去蓝鲸选品查询。查到的 30 天销量汇总进名称，格式是「中文名（xx单）」，不要另加「商品」。FBA 运费用卖家精灵的重量和长宽高，按美客多运费表和同款最低售价计算后写入。没有同款的商品不写入表格。不要自己打开蓝鲸。
提交的返回里已经是下一步要看的列表图、广告图、下一张亚马逊主图，或结束。直接判断，不要为了等详情调用 session_status，也不要再调用 next_listing_batch、next_ad_batch、export_results 或 next_amazon_product。详情和写表在后台进行。只有返回明确要求手动验证时才调用 session_status。不要重复提交同一批。如果图丢了，再调用一次 next_listing_batch 或 next_ad_batch，会把还没判断的同一批发回来。
整店下一次返回已经是下一张主图时，直接 set_keywords，直到返回 end_of_store。单品返回结束后不要再翻店铺。

关键词去掉品牌、尺寸、重量、数量、颜色和空泛用途，保留 “para + 物品”。调用 set_keywords 时同时给出 chinese_name：根据商品标题译成简短中文名，例如「折叠购物车」。表格里的名称用这个中文名，SKU 用关键词。
比对标准和最早一致：颜色不同仍然可以算同一商品。外形、结构和整体构图必须高度相似才是同款。只是同一大类、外形明显不同的，similar 必须为 false。标题已经能明显看出不是同一商品的，优先过滤，不要进看图。图已经明显不是同款的，主代理自己记 false，不要开子代理。confidence 从 0 到 1，不低于 0.8 才进入筛选。判成同款后：广告卡片已经写了 vendidos 的不再打开详情。列表同款仍打开详情抓全量广告；广告商品详情不再读取里面的广告。自己维护销量前 10：卡片或详情销量挤不进前 10 的直接跳过。详情没有 vendido 的不记录。写表取这 10 条。服务端只记录你提交的 similar，不会把未判断或同款改成 false。服务端按标题跳过的候选已经记成 false，不用再提交。

工具会等待十几秒到几十秒，这是风控间隔，不要重复调用同一步。需要手动验证时，在前台窗口完成后重试同一个工具。浏览器最多两个标签。
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
    def open_product(
        seller_id: Annotated[str, Field(description="店铺 id，例如 A1A10HFD4KEZW5")],
        product: Annotated[str, Field(description="用户点名的 ASIN 或商品链接，例如 B0D25PTTTQ")],
    ) -> list[Any]:
        """用户指定了某一个商品时的唯一入口。一次返回该商品主图。禁止先调用 open_store。"""

        return _blocks(invoke("open_store", seller_id=seller_id, product=product))

    @server.tool(structured_output=False)
    def open_store(seller_id: Annotated[str, Field(description="店铺 id。仅当用户没有指定商品时使用")]) -> list[Any]:
        """只遍历整个店铺。用户已经给出 ASIN 或「只处理这个商品」时，禁止调用本工具。"""

        return _blocks(invoke("open_store", seller_id=seller_id, product=""))

    @server.tool(structured_output=False)
    def next_amazon_product() -> list[Any]:
        """只在 open_store 打开整店之后使用。用户指定了商品时禁止调用，主图已经在 open_product 的返回里。"""

        return _blocks(invoke("next_amazon_product"))

    @server.tool(structured_output=False)
    def set_keywords(product_id: str, keywords: str, chinese_name: str = "") -> list[Any]:
        """保存西班牙语关键词和简短中文商品名，打开美客多搜索并直接返回第一批列表主图。"""

        return _blocks(invoke("set_keywords", product_id=product_id, keywords=keywords, chinese_name=chinese_name))

    @server.tool(structured_output=False)
    def next_listing_batch() -> list[Any]:
        """返回搜索结果前两页里的下一批列表主图。标题明显不是同一商品的会先跳过。构图已经明显不是同款的由主代理直接否掉；只有拿不准的再按每 4 张交给 cursor-grok-4.5-low-fast 子代理，合并后再提交。"""

        return _blocks(invoke("next_listing_batch"))

    @server.tool(structured_output=False)
    def submit_listing_matches(matches: list[MatchDecision], model_name: str = "") -> list[Any]:
        """记下列表判断。返回里已经是下一批列表图、广告图、下一张主图，或结束。不要为了等详情调用 session_status。"""

        dumped = [item.model_dump() if hasattr(item, "model_dump") else item for item in matches]
        return _blocks(invoke("submit_listing_matches", matches=dumped, model_name=model_name))

    @server.tool(structured_output=False)
    def next_ad_batch(product_id: str) -> list[Any]:
        """慢速收集三个指定轮播里的主图：两块 Productos relacionados，一块 Quienes vieron este producto también compraron。标题或构图已经明显不是同一商品的先否掉，不进看图子代理。"""

        return _blocks(invoke("next_ad_batch", product_id=product_id))

    @server.tool(structured_output=False)
    def submit_ad_matches(matches: list[MatchDecision], model_name: str = "") -> list[Any]:
        """记下广告判断。返回里已经是下一批广告图、下一张主图，或结束。不要为了等详情调用 session_status。"""

        dumped = [item.model_dump() if hasattr(item, "model_dump") else item for item in matches]
        return _blocks(invoke("submit_ad_matches", matches=dumped, model_name=model_name))

    @server.tool(structured_output=False)
    def session_status() -> list[Any]:
        """查看这一轮的进度，不等浏览器。提交判断或导出之后用它等待下一步。新任务开始时不要调用。"""

        return _blocks(invoke("session_status"))

    @server.tool(structured_output=False)
    def resume_session(session_id: str) -> list[Any]:
        """仅当用户明确说继续上次时，用 session_id 恢复。新任务不要调用。"""

        return _blocks(invoke("resume_session", session_id=session_id))

    @server.tool(structured_output=False)
    def export_results() -> list[Any]:
        """正常路径不要调用。列表和广告结束后服务会自己写表。只有流程中断时才用它补写。"""

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
