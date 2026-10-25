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

项目自带 [`.cursor/mcp.json`](.cursor/mcp.json)，服务名是 `seller-auto`。启用后选定一个能看图的模型，在对话里说：

```text
处理这个店铺 https://www.amazon.com.mx/s?me=卖家ID&marketplaceID=卖家ID
```

亚马逊两次点击之间默认等待 8–15 秒。美客多的搜索、翻页和打开详情之间默认等待 12–25 秒。浏览器最多两个标签。出现验证页时，在弹出的窗口里手动完成，再让对话继续。

颜色不同仍可以算同一商品，外形和整体构图需要高度相似。默认只打开置信度不低于 0.8 的链接。

## 结果

进度写在 `output/<卖家ID>/state.json`，导出文件是同目录下的时间戳 JSON。中断后可以让客户端调用 `resume_session`。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests
```
