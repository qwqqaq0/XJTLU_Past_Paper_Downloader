# XJTLU Library Past Paper PDF Downloader



一个用于在 **XJTLU Library** 上下载往年试卷PDF 的小工具。



---

### 环境要求

- Python 3.9+
- Playwright


---

### 如何使用

#### 1) 运行脚本

在项目目录打开终端，运行：

```bash
python downloader.py
```

#### 2) 在弹出的页面中手动操作

1. 登录你的 XJTLU 账号
2. 找到你想下载的往年试卷并在线打开

此时试卷就会被自动下载到 **Download** 文件夹了

#### 3) 查看保存结果

当程序检测到 **完整 PDF** 后，会自动保存，并在终端输出类似：

```bash
[Saved] .../Download/xxx.pdf
```

默认保存位置：**（脚本所在文件夹）/Download/**

#### 4) 退出程序

把浏览器窗口全部关闭后，脚本会自动结束。