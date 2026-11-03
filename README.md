# 小红书 / RedNote 自动化：注册登录 + 批量发帖

两套独立模块，配置与代码复用：

| 模块 | 入口 | 作用 |
|---|---|---|
| **登录** | `xhs_login.py` / `xhs_batch.py` | 泰国 +66 / 美国 +1 手机号 + Tiger SMS 接码 + 滑块破解，注册登录并导出 cookie |
| **发帖** | `xhs_post.py` | 读 Excel → 检查窗口登录态（未登录自动注册）→ 按后缀发视频/图文 |

跑在比特浏览器本地 API（`127.0.0.1:54345`）之上，通过 CDP 接管窗口，不劫持鼠标键盘。

---

## 一、环境准备

### 1. Python

```
Python 3.10+（本机验证 3.12.6，路径 C:\dev\python\python.exe）
```

### 2. 依赖

```powershell
C:\dev\python\python.exe -m pip install requests playwright numpy pillow openpyxl opencv-python-headless
```

### 3. 比特浏览器

- 启动**客户端**（不是只跑后台服务）
- 确认 API 端口：`Test-NetConnection 127.0.0.1 -Port 54345`
- 每个窗口**必须配 socks5 代理**，裸 IP 访问小红书会直接 `ERR_CONNECTION_CLOSED`
- 内存保护：可用内存 <5% 时会拒绝开窗（`窗口启动失败，内存使用率超出 95%`）

### 4. 接码平台

支持两个平台，用 `SMS_PROVIDER` 切换（协议都是 sms-activate 兼容，代码一套跑）：

| 平台 | 说明 |
|---|---|
| **tiger**（默认） | Tiger SMS 注册充值拿 `api_key`。实测单价：泰国 0.041、美国 0.02~0.05 |
| **hero** | HeroSMS，API 文档见项目根目录 `api-cn.json`。走 `https://hero-sms.com/stubs/handler_api.php` 兼容端点 |

切换方式：

```ini
# .env
SMS_PROVIDER=hero
HERO_API_KEY=你的hero key
HERO_SERVICE=xhs        # hero 平台的小红书 service 代码，不确定就留空用 --probe-service 探测
```

```powershell
# 探测 hero 平台的小红书 service 代码
python xhs_login.py --probe-service
```

两个平台的关键差异（代码里已适配）：

- **库存/价格接口**：tiger 用 `getPricesV3`，hero 用 `getPrices` —— `hero_sms.py` 会先试 V3，失败自动回落 `getPrices` 并归一化格式
- **服务清单**：hero 有 `getServicesList`，探测时按 `xhs/red/rednote` 关键词匹配，比逐个试快
- **mark_ready**：hero 的 `setStatus` 枚举只有 3/6/8（没有 tiger 的 1=就绪），已跳过该调用
- **认证**：都是 query 参数 `api_key`，http 401 视为 `BAD_KEY`

---

## 二、配置 `.env`

项目根目录，程序启动自动加载（支持行内 `#` 注释）：

```ini
# ---------- 比特浏览器 ----------
# 只处理这个分组下的窗口（/group/list 返回的 groupName 全名；留空=全量）
BIT_BROWSER_GROUP=gk7l2dm8sk-默认分组

# ---------- 接码平台 ----------
# 选择：tiger（默认） / hero（新版 api-cn.json 文档在项目根目录）
SMS_PROVIDER=tiger

# tiger 平台
TIGER_SMS_KEY=你的32位key
TIGER_SERVICE=qf               # 小红书在 tiger 平台的 service 代码（qf）

# hero 平台（SMS_PROVIDER=hero 时使用）
# HERO_API_KEY=你的hero key
# HERO_SERVICE=               # hero 平台的小红书 service 代码（留空用 TIGER_SERVICE/探测）

# 泰国（第一优先）
TIGER_COUNTRY_ID=52            # sms-activate 兼容国家表，泰国=52
TIGER_COUNTRY_CODE=66          # 国际电话前缀，程序会自动剥掉

# 美国
TIGER_COUNTRY_ID_US=187
TIGER_COUNTRY_CODE_US=1
TIGER_MAX_PRICE_US=0.05        # 美国号单价上限，超过不买

# 国家优先级：逗号分隔，越靠前越优先（前一个国家 3 个号都失败才换下一个）
# 支持代号 th/us、中文名 泰国/美国、平台编号 52/187、电话前缀 66/1
COUNTRY_ORDER=th,us

# ---------- 取号 / 等码 ----------
CODE_WAIT_SECONDS=90           # 从「点击发送验证码」起算的等码预算
MAX_NUMBERS=3                  # 每个国家最多试几个号
NUMBER_ATTEMPTS=4              # 取号无库存时重试次数
NUMBER_RETRY_INTERVAL=5        # 每次重试间隔（秒）

# ---------- 滑块 ----------
CAPTCHA_RETRIES=2              # 同一弹窗最多试几次（再多会被风控）
CAPTCHA_MIN_MARGIN=0.05        # 区分度低于此值直接换题，不硬拖
CAPTCHA_REFRESH_DELAY_MIN=3    # 换题前随机停顿下限（秒）
CAPTCHA_REFRESH_DELAY_MAX=5    # 换题前随机停顿上限（秒）

# ---------- 发帖 ----------
POST_EXCEL=C:\Users\wangzhan\Desktop\测试.xlsx   # 不填默认桌面\测试.xlsx
POST_SLEEP=10                                    # 每条发完的间隔秒

# ---------- 可选 ----------
# BIT_BROWSER_ID=窗口ID
# TIGER_PROXY=http://127.0.0.1:7890   # 默认直连接码平台，系统代理会 SSL EOF
```

### 参数说明

| 参数 | 默认 | 必填 | 说明 |
|---|---|---|---|
| `BIT_BROWSER_GROUP` | 空 | ❌ | 分组名称，程序先 `/group/list` 换 groupId 再按组分页拉窗口；留空=全量 |
| `SMS_PROVIDER` | `tiger` | ❌ | 接码平台选择：`tiger` 或 `hero` |
| `HERO_API_KEY` | — | hero时✅ | hero 平台的 key（SMS_PROVIDER=hero 时必填） |
| `HERO_SERVICE` | 空 | ❌ | hero 平台的小红书 service 代码，留空用 `--probe-service` 探测 |
| `TIGER_SMS_KEY` | — | ✅ | 缺失直接退出（返回码 2） |
| `TIGER_SERVICE` | `xhs` | ✅ | 实测是 **`qf`**；不确定用 `--probe-service` 探测 |
| `TIGER_COUNTRY_ID` | `52` | ✅ | 泰国 = 52（第一优先国家） |
| `TIGER_COUNTRY_CODE` | `66` | ✅ | 号码前缀，`66813584308` 剥成 `813584308` |
| `COUNTRY_ORDER` | `th,us` | ❌ | **国家优先级**，逗号分隔越前越优先；支持代号/中文名/编号/前缀 |
| `TIGER_COUNTRY_ID_US` | `187` | ❌ | 美国编号（备用） |
| `TIGER_COUNTRY_CODE_US` | `1` | ❌ | 美国前缀，`19295503141` 剥成 `9295503141` |
| `TIGER_MAX_PRICE_US` | `0.05` | ❌ | 美国单价上限，超价的 provider 不买 |
| `CODE_WAIT_SECONDS` | `90` | ❌ | **从点击发送验证码起算**的等码预算（含滑块耗时） |
| `MAX_NUMBERS` | `3` | ❌ | 每个国家最多试几个号；该国全失败就换下一个国家 |
| `NUMBER_ATTEMPTS` | `4` | ❌ | 连 4 次取不到号 → 「当前时段无手机号可用」 |
| `NUMBER_RETRY_INTERVAL` | `5` | ❌ | 取号失败重试间隔 |
| `CAPTCHA_RETRIES` | `2` | ❌ | 同一弹窗最多试几次；高频连试会触发风控 |
| `CAPTCHA_MIN_MARGIN` | `0.05` | ❌ | 缺口区分度低于此值直接换题，不做无意义拖动 |
| `CAPTCHA_REFRESH_DELAY_MIN/MAX` | `3` / `5` | ❌ | 换题前随机停顿，避免机械节奏 |
| `POST_EXCEL` | 桌面`\测试.xlsx` | ❌ | 发帖配置表路径 |
| `POST_SLEEP` | `10` | ❌ | 每条发帖间隔 |
| `POST_ACCOUNT_RETRIES` | `6` | ❌ | 发布遇账号禁言/封号时退出换新号的最大次数（0=不换号） |

### 自检

```powershell
C:\dev\python\python.exe check_config.py
```

输出接码 key（打码）、两国 ID/前缀、限价、Excel 路径是否存在。

---

## 三、启动命令

### 1. 自测（不花号码钱）

```powershell
C:\dev\python\python.exe xhs_login.py --mock-phone 981234567 --browser-id <窗口ID>
```

成功标志：按钮变 `Resend (1xx s)`。

### 2. 单窗口登录

```powershell
C:\dev\python\python.exe xhs_login.py --browser-id <窗口ID>

# 只跑某个国家（默认泰国优先，失败自动切美国）
C:\dev\python\python.exe xhs_login.py --browser-id <ID> --country us

# 只到收码为止
C:\dev\python\python.exe xhs_login.py --browser-id <ID> --dry-run

# 只检查登录态并导 cookie（不开号）
C:\dev\python\python.exe xhs_login.py --browser-id <ID> --scan-only
```

### 3. 批量登录

```powershell
# 先 10 台试水
C:\dev\python\python.exe xhs_batch.py --limit 10 --resume --sleep 15 --min-balance 0.5

# 每轮 20 台，重复执行（--resume 自动跳到未完成的）
C:\dev\python\python.exe xhs_batch.py --limit 20 --resume --sleep 15 --min-balance 0.5

# 排除已手动登录的
C:\dev\python\python.exe xhs_batch.py --exclude 1-20 --limit 20 --resume

# 先扫描登记登录态（不花号码）
C:\dev\python\python.exe xhs_batch.py --scan-logged --sleep 8
```

### 3.1 指定登录哪些窗口

三种方式，按需选一个（都在 `xhs_batch.py` 上）：

**① 按序号（最常用）** —— 不用查 ID，直接写窗口名里的数字：

```powershell
# 只跑 85、86、87 三台
python xhs_batch.py --seqs 85,86,87

# 序号区间 + 零散指定混用
python xhs_batch.py --seqs 80-85,90,95-100

# 指定集合里排除掉某几台
python xhs_batch.py --seqs 85,86,87 --exclude 86
```

**② 按窗口 ID** —— 需要精确控制执行顺序时用：

```powershell
python xhs_batch.py --ids 7c53ad78...,1b232580...,0a0e67d6...
```

**③ 按区间 + 排除** —— 跑大批量时方便：

```powershell
# 1~50 台，但排除已经登录过的 3、7、12
python xhs_batch.py --start 1 --end 50 --exclude 3,7,12
```

**只登录单台**用 `xhs_login.py`：

```powershell
# 按窗口 ID
python xhs_login.py --browser-id 7c53ad787e404d4bb3d1c7a5c2cb196a

# 按名称前缀（自动取第一个匹配的）
python xhs_login.py --name-prefix 小红书测试85
```

**只看登录态、不花号码**（先探一遍再决定跑哪些）：

```powershell
python xhs_batch.py --seqs 85,86,87 --scan-logged
```

逐个扫描 `results/*.json`，已登录的导出 cookie，未登录的标记 `not_logged_in`，
不会取号。扫完看 `batch_summary.json` 就知道哪些要真跑。

**跳过已成功的、断点续跑**：

```powershell
python xhs_batch.py --seqs 80-100 --resume
```

### 4. 发帖（Excel 驱动）

```powershell
C:\dev\python\python.exe xhs_post.py                 # 全部行
C:\dev\python\python.exe xhs_post.py --only 100,99   # 指定窗口
C:\dev\python\python.exe xhs_post.py --only 99 --skip-publish   # 只填不发布
C:\dev\python\python.exe xhs_post.py --no-register   # 未登录不自动注册
```

Excel 五列：

| 列 | 含义 |
|---|---|
| `id` | 窗口序号（对应「小红书测试{id}」） |
| `path` | 视频或图片绝对路径，按后缀自动选发布方式 |
| `title` | 标题（≤20 字，超了截断） |
| `content` | 正文（≤1000 字） |
| `tag` | 标签，多个用空格/逗号分隔，带不带 `#` 都行 |

### 4.1 指定发布哪些作品

`--only` 按 **Excel 里的 id 列**（对应窗口序号）筛选，支持区间写法：

```powershell
# 只发 85、86 两个窗口的稿子
python xhs_post.py --only 85,86

# 区间 + 零散混用
python xhs_post.py --only 85,90-95

# 全量（Excel 里所有行）
python xhs_post.py
```

执行前会打印命中了多少行，**Excel 里没有的序号会明确提示**：

```
--only 88,90-91 -> Excel 命中 0/10 行（Excel 里没有: [88, 90, 91]）
Excel 没有可执行的行
```

**其他常用组合**：

```powershell
# 只填内容不点发布（验证素材/标题/标签，不怕发出去）
python xhs_post.py --only 85 --skip-publish

# 未登录就跳过，不自动注册（省号码钱）
python xhs_post.py --only 85,86 --no-register

# 换一张 Excel
python xhs_post.py --excel "D:\投放	月.xlsx" --only 1-5

# 两条之间多留点间隔（默认 10 秒）
python xhs_post.py --only 85-90 --sleep 30
```

> `--only` 匹配的是 **Excel 的 id 列**，不是窗口名里的数字——
> 两者通常一致（`id=85` → 窗口「小红书测试85」），但 Excel 写错了会找不到窗口。

### 5. 辅助脚本

| 命令 | 作用 |
|---|---|
| `python check_config.py` | 配置自检（含分组名、Excel 路径是否存在）|
| `python bit_client.py` | **打印分组列表 + 解析当前配置的分组 + 该组窗口数** |
| `python check_api.py` | 余额 / 泰国价格库存 |
| `python check_stock.py` | 各 service 库存 |
| `python check_map.py` | Excel 行 → 窗口绑定干跑（不开窗不花钱） |
| `python check_sendcode.py` | 发码判定逻辑自测（-13002 场景） |
| `python check_captcha_drag.py --matrix` | 滑块拖拽离线自检：假页面模拟 k，输出落点误差 + 轨迹统计（不开窗） |
| `python cleanup_numbers.py` | 取消遗留号码 |
| `python test_publish.py <窗口ID> [文件] [标题] [正文]` | 单窗口发帖测试 |

---

## 四、命令参数

### `xhs_login.py`

| 参数 | 说明 |
|---|---|
| `--browser-id` | 比特窗口 ID |
| `--name-prefix` | 按窗口名筛选 |
| `--out` | 结果文件，默认 `login_result.json` |
| `--country {,th,us}` | 只跑指定国家，留空则泰国→美国按序尝试 |
| `--service` | 覆盖 `TIGER_SERVICE` |
| `--probe-service` | 探测可用 service 再跑 |
| `--wait-seconds` | 从点击发码起算的等码预算秒数，默认 90 |
| `--max-numbers` | 每个国家最多试几个号，默认 3 |
| `--dry-run` | 收码后不登录 |
| `--mock-phone` | 固定号码自测 |
| `--scan-only` | 只查登录态导 cookie（读浏览器 cookie jar） |
| `-v` | DEBUG 日志 |

**退出码**：`0` 成功 / `1` 其他失败 / `2` 缺 key / `3` service 探测失败 / `4` 区号切换失败 / `30` 当前时段无号码可用 / `31` 平台异常

### `xhs_batch.py`

| 参数 | 默认 | 说明 |
|---|---|---|
| `--seqs` | — | **按序号指定**跑哪些窗口，支持区间：`85,86,90-95` |
| `--ids` | — | 按窗口 ID 指定，逗号分隔（严格按给定顺序执行） |
| `--prefix` | `小红书测试` | 按窗口名筛选 |
| `--start` / `--end` | 0 | 按名字序号范围过滤 |
| `--exclude` | — | 排除的序号，支持 `1-20,35,40-45` |
| `--limit` | `10` | 最多跑几台（0=不限，在 resume 过滤之后截断） |
| `--resume` | 关 | 跳过 `batch_summary.json` 里已成功的 |
| `--scan-logged` | 关 | 只扫描登录态并登记 |
| `--shuffle` / `--seed` | 关 / `20261028` | 打乱顺序 |
| `--retry` | `0` | 单台失败重试次数 |
| `--sleep` | `12` | 两台间隔秒（必须留，防内存超限） |
| `--timeout` | `480` | 单台超时 |
| `--min-balance` | `0` | 余额低于此值停止 |

### `xhs_post.py`

| 参数 | 默认 | 说明 |
|---|---|---|
| `--excel` | `config.POST_EXCEL` | 发帖配置表 |
| `--only` | — | 只发这些窗口序号（Excel id 列），支持区间 `85,90-95` |
| `--skip-publish` | 关 | 只填内容不点发布 |
| `--no-register` | 关 | 未登录不自动注册 |
| `--wait-seconds` / `--max-numbers` | 继承配置 | 注册时用 |
| `--sleep` | `config.POST_SLEEP` | 两条间隔 |

---

## 五、输出文件

| 路径 | 内容 |
|---|---|
| `results/NNN_xxxxxxxx.json` | 每台登录结果：`ok`、`phone`、`local`、`code`、`cookies`、`usage`、`submitted` |
| `logs/NNN_xxxxxxxx.log` | 每台完整日志 |
| `batch_summary.json` | 批次汇总：逐台状态 + 余额变化 |
| `success_log.jsonl` | 每次成功一行（用于“上一个成功窗口”提示） |
| `post_results.json` | 发帖结果：seq / kind / ok / reason / 耗时 |
| `captcha_assets/` | 滑块图片缓存 + `strategy.json` |

`usage` 字段表示这个号**到底有没有被用掉**（关系到钱）：

| 值 | 含义 | 钱 |
|---|---|---|
| `used` | 登录成功，验证码确实用上了 | 花得值 |
| `refunded` | 码没用上，但退号成功 | 已退回 |
| `wasted` | 码没用上且平台不让退（`SMS_RECEIVED`） | 真损失 |

`submitted` 表示验证码被提交给小红书的次数（0 = 根本没填进去）。

---

## 六、运行流程

### 窗口从哪来

```
POST /group/list  {page:0, pageSize:100, all:true}
  └─ 拿到 [{id, groupName, browserCount}, ...]
       └─ 用 .env 的 BIT_BROWSER_GROUP 匹配出 groupId（精确优先，其次唯一包含）
            └─ POST /browser/list  {page:0, pageSize:100, groupId}
                 └─ 循环翻页直到 len(batch) < 100 或累计 >= totalNum
```

要点：

- `/group/list` 的分组名字段是 **`groupName`**（不是 `name`），代码已兼容两种
- `pageSize` 上限 **100**，传 200 也只返回 100 条，所以内部固定按 100 翻页
- `page` 从 **0** 开始
- 模糊匹配到多个分组会直接报错让写全名，不会瞎选

### 登录态怎么判

先读**浏览器 cookie jar**（`ctx.cookies()`，不是导出的 cookie 文件），再用页面确认：

```
1. 没有 web_session  → 直接判定未登录（不发请求，<1 秒）
2. 有 web_session    → 打开 creator 发布页确认服务端认不认
                        └─ 被弹回 login?401 → cookie 已失效，需重新登录
```

实测过的坑：**cookie 在 ≠ 登录有效**。测试87/85 浏览器里 `web_session` 还在，但页面直接被 401 弹回登录页。所以 cookie 只能当前置筛选，不能单独定论。

`--scan-only` 的返回会区分两种情况：

```json
{"ok": false, "cookie_present": true, "cookie_stale": true, "login_cookie": "a1(52B), web_session(38B), ets(13B)"}
```

### 登录

1. 打开比特窗口（按分组拉取）→ CDP 接管 → 进 `https://www.rednote.com/login`
   （必须国际版，`xiaohongshu.com` 区号固定 +86）
2. **按 `.env` 的 `COUNTRY_ORDER` 顺序**依次尝试（默认 `th,us` 即泰国优先），前一个国家 3 个号都失败才换下一个；库存只打日志参考，不参与排序
3. 切区号 → 取号（带 maxPrice）→ **剥掉国家前缀** → 填号
4. 点 `Get code`，记下时刻 **t_send**：
   - 返回 **471** → 弹滑块 → 识别缺口 + 拟人拖动 → 通过后前端重发 → **200**
   - 返回 **200 但 `code:-13002`**（发送失败）→ **立即退号换下一个**
5. 等码预算 = `90s - (now - t_send)`，滑块耗时也算在内：
   - 剩余 <15s → 直接退号换号（不白等）
   - 否则轮询 `getStatus` 直到收到或超时
6. 超时/失败 → **退号退款**：平台要求号存活 ≥125 秒，不够就等够再退（`cancel_with_retry`），退成功才换下一个号
7. 收到码 → 填码 → 登录 → 跳过性别/兴趣引导 → 导出 cookie
8. 单国 3 个号都失败 → 换下一个国家；两国都失败 → **当前时段无手机号可用，直接停**

### 发帖

```
开窗 → 读浏览器 cookie 判断登录态
  ├─ cookie 有效且页面能进 → 直接发
  └─ 否则 → 切区号 → 取号 → 发码 → 滑块 → 登录（同上，含国家回退）
发帖
  ├─ .mp4/.mov/... → 上传视频 tab（CDP setFileInputFiles，突破 50MB 限制）
  ├─ .jpg/.png/... → 上传图文 tab
  ├─ 等上传完成（缩略图 + 文本静默 4.5s）
  ├─ 填标题 → 填正文 → 逐个 #标签 回车确认
  └─ 点发布：等 submit-disabled=false → 通过 _sr 点按钮 → 坐标点击兜底
       ├─ 「图片上传中，请稍后」→ 等 5 秒重点，最多 6 次
       ├─ 「发布成功」→ 关闭页面
       └─ 账号异常（禁言/封号）→ 清 cookie 退登 → 重注册 → 重发
            └─ 新号仍被禁 → 继续退出换新号，直到能正常发布（上限 POST_ACCOUNT_RETRIES）
```

### 滑块怎么破的

缺口定位只保留 OpenCV 内容匹配一套（旧 scipy 形状方案已移除，`xhs_captcha_cv.py`）。

- 背景图 `*_hole.png` 有两块同形暗块（目标洞 + 干扰影子），**轮廓几乎完全一样**（49 组历史样本里 18 组两者形状 IoU 都是 0.99），所以光看形状分不出来
- 真缺口是**被压暗、模糊过的拼图块本体**，干扰块是画面另一处的同形暗斑 → 判据用「拼图块内容 ↔ 缺口内内容」的零均值归一化相关（`cv2.matchTemplate(TM_CCOEFF_NORMED, mask=alpha)`），亮度差被 CCOEFF 归一化抵消
- 候选定位走 `cv2.morphologyEx + connectedComponentsWithStats`，按面积/边长/长宽比/填充率过滤
- 先拖 2 个近距离点读 `translate3d` 实测映射系数 k（0.80~1.37），做过原点**最小二乘**拟合；残差 >25% 或 k 越界说明读数不可信，按舞台宽度兜底而不是硬算
- 拖动走**拟人轨迹**：最小加加速度速度曲线（起止速度为 0）/ 步间隔抖动 + 2~3 次微停顿 / y 轴 ±1.6px 抖动 / 45% 概率过冲 2~6px 再回拉 / 松手前手抖找位；落点偏差 >2px 才回拉修正，最多两轮
- 失败点「换一换」并轮换策略 `top / right / inner`，命中策略写入 `strategy.json`
- 拼图块图形在元素内左侧有 **13px 透明边**（题库固定，51 组样本无一例外），算位移时必须扣掉，否则整体偏右约 1/4 框；代码按 alpha bbox 动态读取，并按「元素宽 / 原图宽」比例换算（元素 58px 时约 8.2px）
- 区分度 `margin < 0.05` 时直接换题、不硬拖；同一弹窗最多试 2 次，换题前随机停 3~5 秒——高频连试会触发风控，后面分数再高也一律不过

### 发布按钮为什么这么绕

按钮在 `<xhs-publish-btn>` 的**闭合 Shadow DOM** 里（`shadowRoot === null`），普通选择器和 locator 都进不去。三级策略：

1. 等宿主属性 `submit-disabled="false"` + `submit-loading!="true"`
2. 用组件自己挂的 `container._sr`（ShadowRoot 引用）取到按钮点
3. 兜底按宿主 62%/60% 坐标点

---

## 七、常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `找不到分组 'xxx'；现有分组: [...]` | `.env` 里的分组名不对 | 跑 `python bit_client.py` 看分组列表，写 `groupName` 全名 |
| `分组名 '默认分组' 匹配到多个` | 模糊匹配命中多个 | 写全名，如 `gk7l2dm8sk-默认分组` |
| 明明是已登录，脚本却判未登录 | cookie 残留但已被服务端 401 | 看 `--scan-only` 输出的 `cookie_stale`，是就重新注册/登录 |
| `窗口启动失败，内存使用率超出 95%` | 比特内存保护 | 关掉其他程序；加大 `--sleep` |
| `ERR_CONNECTION_CLOSED` / 页面空白 | 窗口代理失效或裸 IP | 换代理或换窗口 |
| `ERR_SOCKS_CONNECTION_FAILED` | socks5 挂了 | 换窗口 |
| `NO_NUMBERS` ×4 → 当前时段无手机号可用 | 号池缺货 | 等补货；调大 `NUMBER_ATTEMPTS` |
| `send_code` 返回 `code:-13002` | 平台拒绝该号发码 | 已自动秒换号 |
| 等码超时后卡住一会才换号 | 平台要求号存活 ≥125 秒才可退 | 正常现象，`cancel_with_retry` 在等够时间，钱会退回 |
| 滑块一直不过 | 轨迹被判机器 / 选错洞 | 自动换题重试 2 次；仍不过就换窗口（别原地死磕） |
| 滑块通过但落点偏右 | 拼图块左侧 13px 透明边没扣 | 已在代码里按 alpha bbox 扣除 |
| 提示「因违反社区规则禁止发笔记」/ 账号禁言 | 账号被限制发布 | 已识别为账号异常，自动退号换新号重发（上限 POST_ACCOUNT_RETRIES） |
| 发帖提示「图片上传中，请稍后」 | 点太早 | 已改为等缩略图 + 文本静默再点，且会自动重点 |
| 点发布没反应 | 点到侧边栏「发布笔记」了 | 已改用 `_sr` 精确定位 |
| 登录成功但 `ok:false` | 停在完善资料页 | 已自动点 Continue |
| 控制台中文乱码 | Windows GBK | `$env:PYTHONIOENCODING='utf-8'` |

---

## 八、目录结构

```
python-demo/
├─ bit_client.py        分组 + 窗口列表（/group/list + /browser/list 分页）
├─ xhs_login.py         登录/注册主流程（泰国优先、多国家回退、cookie 登录判定）
├─ xhs_captcha.py       滑块流程（标定/拟人拖拽/策略轮换/退号）
├─ xhs_captcha_cv.py    缺口定位内核（OpenCV 内容匹配）
├─ xhs_batch.py         批量登录（子进程隔离、--resume/--exclude）
├─ xhs_publish.py       发帖核心（上传/填写/发布/判定）
├─ xhs_post.py          Excel 驱动的批量发帖
├─ tiger_sms.py         接码平台客户端（maxPrice、cancel_with_retry）
├─ config.py            配置加载 + 国家列表 + 选择器
├─ check_config.py      配置自检
├─ check_stock.py       service 库存
├─ check_map.py         Excel→窗口 绑定干跑
├─ check_captcha_drag.py 滑块拖拽离线自检（假页面 + 轨迹统计）
├─ check_cv_visual.py   滑块候选框可视化
├─ check_margin_skip.py margin 分布统计
├─ check_piece_offset.py 拼图块透明边测量
├─ cleanup_numbers.py   取消遗留号码
├─ test_publish.py      单窗口发帖测试
├─ .env                 私有配置（勿提交）
├─ results/ logs/       登录结果与日志
├─ captcha_assets/      滑块图片 + 策略记忆
└─ probe*/              探测脚本与截图（可删）


---

## 九、注意事项

1. **分组隔离**：脚本只处理 `BIT_BROWSER_GROUP` 指定分组下的窗口。改分组后先跑
   `python bit_client.py` 确认解析到的 groupId 和窗口数对不对。
2. **余额**：100 台按 1.2 号/台估算约 5 美元；`--min-balance` 会兜底。
3. **不要并发**：单机并发会同时吃内存和同 IP 段风控额度；提速用多机 `--ids` 分片。
4. **虚拟号到达率低**：`send_code` 200 但收不到码很常见，属号池质量问题，不是程序错。
5. **退号有等待**：等码超时后要等够平台 125 秒最小存活期才退得掉，单个号最坏约 3 分钟。
6. **代理是命门**：窗口代理一挂整台白跑，批量前先 `--limit 2` 探路。
7. **cookie 在≠登录有效**：浏览器里有 `web_session` 也可能已被服务端踢掉，
   判断登录态必须以页面能否进站为准（代码已这样处理）。
8. **配置优先级**：命令行参数 > `.env` > 代码默认值。
