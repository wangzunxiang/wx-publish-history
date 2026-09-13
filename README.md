# 公众号发布历史查询工具 (WeChat MP Article History Tool)

> **个人微信公众号** 已发布文章 / 发表记录 查询工具（本地 Web 界面）
>
> 适用于**订阅号、服务号、个人主体、未认证账号**。模糊搜索、日期筛选、排序、
> 导出 CSV/Excel/Markdown，数据仅存本机。

## 简介

本工具查询**你自己的**微信公众号后台「发表记录」——也就是该公众号历史发布的
全部文章（标题、发布时间、摘要、文章链接、作者），并提供一个本地网页界面：

- 🔍 **模糊搜索**：按标题/摘要/作者搜，支持多关键词（空格分隔，需同时命中），命中高亮
- 📅 **日期区间筛选** + 时间升/降序排列
- 📤 **导出 CSV**（带 BOM，Excel 直接打开）**/ Markdown**
- 🔄 一键刷新全量数据
- 🔐 会话持久化：扫码登录一次后自动免扫码，直到微信会话过期

**不需要 AppID / AppSecret，不依赖任何第三方接口，无需部署服务器**，
克隆后在本机跑起来即可（支持 Windows / Linux / macOS）。

## 关键词 / Keywords

微信公众号文章查询 · 公众号历史文章 · 公众号发表记录 · 订阅号文章列表 ·
文章搜索工具 · 导出公众号文章标题 · WeChat Official Account article history ·
WeChat MP publish record · subscription account (订阅号) article search ·
article list query · offline search · 本地查询 · 免 AppSecret · 免服务器

**Tags**: wechat, weixin, 公众号, 订阅号, official-account, article, history,
search, 文章查询, 发表记录, csv-export, local-web-app, playwright

## 为什么不用 OpenAPI
OpenAPI 的 `freepublish/batchget`（读已发布列表）对**个人主体未认证订阅号**
返回 48001（无接口权限），所以本工具改走公众号**后台网页会话**：
用本机 Chromium 打开后台登录页 → 手机微信扫码 → 用该会话读取后台页面内嵌的
发表记录数据。**不需要 AppID/AppSecret**。

## 依赖
- Python 3.8+
- 浏览器：自动探测 Chromium / Chrome / Edge；都找不到时自动用
  Playwright 自带内核（需先执行 `playwright install chromium`）
- Playwright：
  ```bash
  # Linux / macOS
  python3 -m venv .venv
  .venv/bin/pip install playwright
  .venv/bin/playwright install chromium     # 可选，装了就不用系统浏览器

  # Windows (PowerShell)
  py -3 -m venv .venv
  .\.venv\Scripts\pip install playwright
  .\.venv\Scripts\playwright install chromium
  ```

## 使用
```bash
# Linux / macOS
./wx_history            # 默认端口 8765
./wx_history 9000       # 指定端口

# Windows：双击 wx_history.bat，或
python wx_history.py 8765
```
1. 浏览器自动打开 http://127.0.0.1:8765
2. 页面自动显示登录二维码，手机微信扫码并确认
3. 登录成功后自动拉取全部发表记录，即可搜索
4. 会话保存在 wx_state.json：未过期时下次启动自动恢复，免扫码；
   过期后点「刷新数据」会要求重新扫码

## 功能
- 模糊搜索：标题/摘要/作者，空格分隔多关键词（需同时匹配），命中高亮
- 日期区间筛选、时间升/降序
- 导出 CSV（带 BOM，Excel 直接打开）/ Markdown
- 一键刷新（重新从后台拉取全量）

## 文件
| 文件 | 说明 |
|---|---|
| wx_history.py | 主程序（单文件，仅 Python 标准库 + playwright） |
| wx_history | Linux/macOS 启动脚本（自动选 venv 的 python） |
| wx_history.bat | Windows 启动脚本（双击运行） |
| .venv/ | playwright 虚拟环境 |
| wx_state.json | 后台会话（自动生成，**等同登录凭证，勿外传；跨系统不通用**） |
| articles.json | 文章数据缓存 |
| debug.log | 调试日志 |

## 注意
- 仅监听 127.0.0.1，局域网其他机器无法访问
- 能查到 2018 年「发布能力」上线之后、后台「发表记录」页能看到的全部历史
- 扫码登录只在本机 Chromium（headless）中进行，手机确认即完成，全程无凭据落地
- 换电脑（尤其 Linux↔Windows）时 wx_state.json 不通用，重新扫码即可；
  articles.json 可直接拷贝使用
