# seller-auto

本地 MCP 服务。用阿根廷西班牙语、布宜诺斯艾利斯时区和地理位置打开浏览器，从亚马逊墨西哥店铺找出非服装、非食品商品，再去美客多阿根廷找有销量的同款。

关键词和主图是否同款由 Cursor 等客户端里当前选中的模型判断。项目不保存、也不调用模型 API Key。

## 准备

需要 Python 3.13。

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

## 在 Cursor 里使用

项目自带 [`.cursor/mcp.json`](.cursor/mcp.json)，服务名是 `seller-auto`。启用后，检索元素和对比主图都使用 grok 4.5 low fast，模型名写 `cursor-grok-4.5-low-fast`。在对话里说：

```text
处理这个店铺id：A1A10HFD4KEZW5
```

服务会自行打开 `https://www.amazon.com.mx/s?me=店铺id&marketplaceID=店铺id`。对话里只需要给出店铺 id。

只处理一个商品时说「处理这个店铺id：A1A10HFD4KEZW5，只处理这个商品：B0D25PTTTQ」。这样只会打开这一条，不会先跑整店。

亚马逊两次点击之间默认等待 8–15 秒。美客多的搜索、翻页和打开详情之间默认等待 12–25 秒。浏览器默认在后台运行，不弹出窗口，最多两个标签。它使用完整的 Chrome for Testing，而不是精简的 headless shell；语言、时区和地理位置仍是阿根廷。请求头和页面里的自动化标记会被去掉。出现验证页时，会临时打开前台窗口，完成后回到后台。需要一直看窗口时，给后台进程加上 `--foreground`。

这些风控间隔保持不变。列表和广告图每批最多 24 张。卡片上已经写了销量的直接记录；没写的仍打开详情，因为低销量往往只出现在详情页。一次工具返回里会带上下一批要判断的图。各步骤日志写在 `logs/seller-auto.log`。

颜色不同仍可以算同一商品，外形和整体构图需要高度相似。先按相似、销量和一层广告位把链接筛完，再按相似度取前 10 条。进入筛选的置信度不低于 0.8。

## 结果

每次 `open_store` 都会新开一轮，进度写在 `output/<卖家ID>/<开始时间>/`。同一卖家以前的结果留在各自的目录里，不会被接着跑。只有用户明确说继续上次时，才调用 `resume_session`。

导出时会把销量前 10 的美客多链接拿到蓝鲸选品逐条查询。查到的近 30 天销量汇总后写入名称，格式是「中文名商品（xx单）」。尾程运费按蓝鲸的阿根廷官方海外仓运费表计算，写入 FBA 运费的是比索金额，表格再用汇率折成人民币。计费重量取实重和体积重量的较大值，体积重量是长宽高厘米乘积除以 4000。重量和尺寸优先用卖家精灵记录的包装。第一次打开蓝鲸如果还没登录，会短暂弹出窗口，登录完成后回到后台。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests
```
