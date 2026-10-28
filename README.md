# seller-auto

本地 MCP 服务。用阿根廷西班牙语、布宜诺斯艾利斯时区和地理位置打开浏览器，从亚马逊墨西哥店铺找出非服装、非食品商品，再去美客多阿根廷找有销量的同款。

关键词和主图是否同款由 Cursor 等客户端里当前选中的模型判断。项目不保存、也不调用模型 API Key。

## 准备

需要 Python 3.13。

macOS：

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

Windows：

没有 `py` 启动器时，先装 Python 3.13，装完新开一个终端：

```bat
winget install --id Python.Python.3.13 -e --scope user --accept-package-agreements --accept-source-agreements
```

官方 PyPI 和 Playwright 的下载源在国内往往很慢。依赖走清华源，Chrome for Testing 走 npmmirror：

```bat
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
set PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
.venv\Scripts\python.exe -m playwright install chromium
```

新终端里仍然没有 `py` 时，把创建虚拟环境的那一行换成：

```bat
"%LOCALAPPDATA%\Programs\Python\Python313\python.exe" -m venv .venv
```

官方安装包只有 `python.exe`，没有 `python3`。不要改 [`.cursor/mcp.json`](.cursor/mcp.json)。在 Python 安装目录做硬链接，让 `python3` 指向同一个解释器：

```bat
mklink /H "%LOCALAPPDATA%\Programs\Python\Python313\python3.exe" "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
```

用户 PATH 里 `Python313` 要排在 `WindowsApps` 前面，避免命中微软商店的 `python3` 占位程序。Cursor 在安装 Python 之前就已经打开时，它的 PATH 里没有 `Python313`，`python3` 会落到商店占位程序并马上退出。只重连 MCP 不够，需要完全退出 Cursor 再打开。硬链接必须和 `python.exe` 放在同一目录，否则解释器找不到旁边的 DLL。

换到一台新电脑时，把项目拷过去或重新克隆，不要拷 `.venv`。虚拟环境和 Chrome for Testing 都在新电脑上按上面的命令重装。`output/` 和 `logs/` 只是以前的结果和日志，不拷也能开始。新电脑需要能打开亚马逊墨西哥、美客多阿根廷、卖家精灵、1688 和蓝鲸选品。用 Cursor 打开项目后，按下一节启用自带的 MCP。

卖家精灵账号写在项目根目录的 `.env.local`，这个文件不会进 git：

```text
E2E_USERNAME=卖家精灵账号
E2E_PASSWORD=卖家精灵密码
```

没有这个文件就查不了包装和 1688 货源。macOS 上已经登录过的 `data/profile` 不要拷到 Windows。两台都是 Mac 时可以拷 `data/profile` 带上登录态，第一次仍要在前台确认。卖家精灵靠 `.env.local` 自己登录。蓝鲸选品和 1688 第一次打开时如果还没登录，会短暂弹出前台窗口，登录完成后回到后台。美客多出现验证页时同样在前台完成。

## 在 Cursor 里使用

项目自带 [`.cursor/mcp.json`](.cursor/mcp.json)，服务名是 `seller-auto`。它用 `python3` 运行 `scripts/launch_mcp.py`，再转到虚拟环境里的解释器。Windows 上按上一节做成 `python3` 硬链接，不要改 `command`，并先重启 Cursor。看图、写关键词和判断同款由对话里当前选中的模型完成。提交时 `model_name` 填当前模型名。在对话里说：

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

导出时会把销量前 10 的美客多链接拿到蓝鲸选品逐条查询。查到的近 30 天销量汇总后写入名称，中文名在上，下一行是「（xx单）」。尾程运费按蓝鲸的阿根廷官方海外仓运费表计算，写入 FBA 运费的是比索金额，表格再用汇率折成人民币。计费重量取实重和体积重量的较大值，体积重量是长宽高厘米乘积除以 4000。重量和尺寸优先用卖家精灵记录的包装。第一次打开蓝鲸如果还没登录，会短暂弹出窗口，登录完成后回到后台。

## 测试

macOS：

```bash
.venv/bin/python -m unittest discover -s tests
```

Windows：

```bat
.venv\Scripts\python.exe -m unittest discover -s tests
```
