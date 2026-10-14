# 第三方组件与服务说明

## Python 依赖

- Mutagen — GPL-2.0-or-later
- Pillow — MIT-CMU
- opencc-python-reimplemented — Apache-2.0
- PyInstaller（仅构建）— GPL-2.0-or-later，附带 bootloader exception

具体版本以 `requirements.txt` 和 `requirements-dev.txt` 为准。发布二进制文件时应保留对应许可证文本。

## 在线服务

程序可能访问以下服务以检索公开的歌曲元数据、封面或歌词：

- 网易云音乐
- QQ音乐
- 酷狗音乐
- Apple iTunes Search API
- LRCLIB
- MusicBrainz
- Cover Art Archive

这些服务不隶属于本项目。其可用性、数据准确性和使用条件由各服务提供方决定。部分接口不是面向第三方应用的稳定公开 API，可能在没有通知的情况下发生变化。
