# XJTLU Library Past Paper PDF Downloader

用于按课程代码自动下载 XJTLU ETD 往年试卷 PDF。

## 环境要求

- Python 3.9+
- Playwright

## 配置文件

默认读取 `downloader.config.json`：

```json
{
  "course_codes": ["CPT102", "CPT104", "INT102", "INT104"],
  "login_timeout": 300,
  "max_clicks": 0,
  "headless": false
}
```

- `course_codes`：一次性下载的课程代码列表
- `login_timeout`：等待 XJTLU 登录的秒数
- `max_clicks`：每门课最多打开多少条搜索结果；`0` 表示打开当前搜索结果页显示的全部链接
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
6. 依次搜索配置中的每个课程代码
7. 对每门课，依次把当前搜索结果页中显示的每条结果链接打开到新标签页，避免返回时清空搜索条件
8. 在详情页点击 `View Online`
9. 检测到完整 PDF 后保存到 `Download` 文件夹

浏览器登录态会保存在 `.browser_profile` 中，通常登录成功一次后，后续运行可以复用登录状态。
