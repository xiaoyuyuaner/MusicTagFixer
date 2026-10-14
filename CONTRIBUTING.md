# 贡献指南

感谢你考虑改进 MusicTagFixer。

## 提交问题

请先搜索已有 Issue，并尽量提供：

- 操作系统与版本；
- 使用源码版还是 Windows 免安装版；
- 音频格式；
- 可脱敏的文件名和日志；
- 预期结果与实际结果；
- 是否能够稳定复现。

请勿公开上传包含版权音乐、账号信息、Cookie、Token 或其他敏感数据的文件。

## 本地开发

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m py_compile music_tag_fixer.py
python music_tag_fixer.py
```

## Pull Request

- 一个 PR 尽量只解决一个问题；
- 不得删除“保留原文件”和歌手强绑定等安全规则；
- 新增在线来源时必须设置超时、可信度门槛与失败回退；
- 不得提交密钥、账号凭证或绕过平台限制的代码；
- 涉及标签写入时，应覆盖 MP3、FLAC、M4A 的回归测试；
- 更新用户可见行为时，请同步更新 README 和 CHANGELOG。

## 代码风格

- 使用 Python 3.10+；
- 优先使用标准库和类型标注；
- 网络错误不得导致整个批次中断；
- 所有输出默认写入新文件，不覆盖用户原件。
