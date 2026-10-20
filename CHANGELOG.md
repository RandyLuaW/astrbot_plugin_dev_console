# 更新日志

所有重要变更记录于此。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [1.1.0] - 2026-10-20

### 新增

- 首个公开版本。
- `dev_read_file` — 只读，支持 offset 分段读大文件
- `dev_write_file` — 只能写 `staging/`，不碰运行目录
- `dev_list_staging` — 看候选清单
- `dev_probe` — compileall + 另端口启动 + 健康检查 + 出 patch
- `dev_apply` — 落地，要求上次探针 PASS 且候选指纹未变
- `dev_status` / `dev_journal` — 工位状态与工程日志
- `/dev` 指令

