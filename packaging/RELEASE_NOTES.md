个人桌面版 v0.1.2，四平台审查修复版。

- Windows x64、macOS Apple Silicon、macOS Intel、Linux x64 分别在原生 GitHub runner 上构建。
- 每个平台均运行回归测试，并启动打包后的引擎与 GUI 验证通信和退出。
- 修复任务收尾时提前释放、长结果截断、取消通知缺失、迟到查询覆盖、编辑权限恢复及任务号复用。
- 旧版数据库升级前自动备份，长结果按分片耐久投递。
- Intel macOS 使用静态 OpenSSL 构建加密依赖，避免打包库冲突。
- SHA256SUMS.txt 包含本次所有平台包的校验和。

Windows 下载 ZIP 并解压，运行 OnCallAgent.exe，保留同目录依赖。
macOS 根据芯片选择 arm64 或 x64 ZIP，解压后将 OnCallAgent.app 拖入“应用程序”。包采用临时签名，尚未经 Apple Developer ID 签名和公证，首次启动可能需在系统“隐私与安全性”中允许打开。
Linux x64 下载 tar.gz 并解压，运行 OnCallAgent/OnCallAgent，保留整个目录。构建和启动验证使用 Ubuntu 24.04，需图形桌面及已解锁的 Secret Service 凭据服务（例如 GNOME Keyring）。

各平台均需本机安装并认证 Claude Code，然后配置飞书企业自建机器人并配对本人私聊。Windows 包未做发行签名，真实飞书与模型账号仍需在本机联调。
