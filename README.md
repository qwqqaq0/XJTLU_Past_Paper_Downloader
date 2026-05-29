# XJTLU Library Past Paper PDF Downloader

用于按课程代码自动下载 XJTLU ETD 往年试卷 PDF。

## 环境要求

- Python 3.9+
- Playwright

## 配置文件

默认读取 `downloader.config.json`：

```json
{
  "course_codes": [],
  "course_prefixes": ["SAT", "CAN", "CPT", "EEE", "INT", "MEC"],
  "login_timeout": 300,
  "max_results": 0,
  "max_pages": 0,
  "detail_wait_ms": 800,
  "view_online_wait_ms": 5000,
  "final_wait_ms": 3000,
  "dry_run": false,
  "headless": false
}
```

- `course_codes`：一次性下载的具体课程代码列表，例如 `["INT102", "CPT102"]`
- `course_prefixes`：一次性下载的课程代码前缀列表，例如 `["SAT", "CAN", "CPT"]`
- `login_timeout`：等待 XJTLU 登录的秒数
- `max_results`：每个课程代码或前缀最多下载多少条搜索结果；`0` 表示不限制
- `max_pages`：每个课程代码或前缀最多翻多少页；`0` 表示自动翻到最后一页
- `detail_wait_ms`：打开详情页后的等待时间，默认 `800`
- `view_online_wait_ms`：点击 `View Online` 后等待 PDF 保存完成的最长时间，默认 `5000`；如果提前保存成功会立刻继续
- `final_wait_ms`：全部任务完成后最后等待 PDF 响应的时间，默认 `3000`
- `dry_run`：只搜索、列出结果和翻页，不打开详情页、不下载 PDF，适合测试分页
- `headless`：是否无界面运行；首次登录不要设为 `true`

## 使用方法

双击 `run.bat`，脚本会优先使用 `downloader.config.json` 中的课程列表。

也可以在 PowerShell 中运行：

```powershell
.\.venv\Scripts\python Downloader.py
```

命令行传入课程代码时，会覆盖配置文件里的 `course_codes`：

```powershell
.\.venv\Scripts\python Downloader.py INT102 CPT102
```

也可以直接传课程前缀：

```powershell
.\.venv\Scripts\python Downloader.py --prefix SAT,CAN,CPT,EEE,INT,MEC
```

如果网络和页面加载都比较稳定，可以继续调低等待时间来加速：

```powershell
.\.venv\Scripts\python Downloader.py --prefix INT --detail-wait-ms 500 --view-wait-ms 3000 --final-wait-ms 1000
```

测试搜索和自动翻页但不下载：

```powershell
.\.venv\Scripts\python Downloader.py --prefix INT --max-pages 2 --max-results 21 --dry-run
```

也可以指定其他配置文件：

```powershell
.\.venv\Scripts\python Downloader.py --config my.config.json
```

## 自动流程

脚本会按照当前 ETD 页面流程自动操作：

1. 打开 `https://etd.xjtlu.edu.cn/index.html#/index`
2. 判断首页是否显示 `Login Successfully`
3. 如果未登录，点击 `Login` 并等待 XJTLU 认证完成
4. 点击 `Past Exam Papers`
5. 如果出现 `User Agreement` 页面，点击 `Agree`
6. 依次搜索配置中的每个课程代码或课程前缀
7. 对每个搜索词，依次把当前搜索结果页中显示的每条结果链接打开到新标签页，避免返回时清空搜索条件
8. 当前页处理完后自动点击下一页，直到没有下一页或达到 `max_pages`
9. 在详情页点击 `View Online`
10. 根据搜索结果表中的 `Paper Code` 分类保存到 `Download/课程前缀/课程代码/`，例如 `Download/CPT/CPT104/`

浏览器登录态会保存在 `.browser_profile` 中，通常登录成功一次后，后续运行可以复用登录状态。
