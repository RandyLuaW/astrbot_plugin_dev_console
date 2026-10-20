# 开发工位：沙箱改码 → 探针验证 → 人工确认后落地

> AstrBot 插件 · 沙箱改码 → 探针验证 → 人工确认后落地

## 它解决什么问题

改的是模型自己正在跑的进程。改 `.py` 不会热重载，重启崩了就再也起不来，连「报告失败」的能力都没有。

所以写入与落地必须分离，中间隔一个活着的旁观进程——探针实例。

## 功能

- `dev_read_file` — 只读，支持 offset 分段读大文件
- `dev_write_file` — 只能写 `staging/`，不碰运行目录
- `dev_list_staging` — 看候选清单
- `dev_probe` — compileall + 另端口启动 + 健康检查 + 出 patch
- `dev_apply` — 落地，要求上次探针 PASS 且候选指纹未变
- `dev_status` / `dev_journal` — 工位状态与工程日志
- `/dev` 指令

## 工作机制

dev_write_file → staging/<候选>
        ↓
dev_probe → 另一个端口起一个实例，健康检查，PASS/FAIL + patch
        ↓（PASS 且指纹未变）
dev_apply → 写入运行目录

落地不做二次验证，靠指纹保证「验过的就是落地的」。
候选路径必须落在 astrbot/ 或 main.py 下，全部工具仅管理员可用。

## 安装

### 方式一：插件市场（推荐）

WebUI 插件市场搜索「开发工位」。

### 方式二：Releases 下载

从 [Releases](../../releases) 下载 zip，WebUI → 插件 → 上传安装。

### 方式三：手动克隆

```bash
cd AstrBot/data/plugins
git clone https://github.com/RandyLuaW/astrbot_plugin_dev_console.git
```

无额外 Python 依赖。

## 配置项

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 启用开发工位。关闭后不再注入工位提示，dev_* 工具与 /dev 指令一律拒绝；已经写进 staging 的候选文件仍保留，重新打开后可以继续探针。 |
| `inject_hint` | bool | `true` | 是否在管理员私聊里每轮注入「你有工位、五步流程」的提示。关掉后她仍能自己调用工具，但不会主动想起来有这么个地方。 |
| `max_read_lines` | int | `600` | dev_read_file 单次最多返回多少行，也是她传 max_lines 时的硬上限。读大文件靠 offset 分段，别一次读爆上下文。 |
| `max_write_bytes` | int | `204800` | dev_write_file 单次写入的字节上限，默认 200KB。超了直接拒绝、不落盘，防止一次塞进整份超大文件。 |
| `probe_timeout` | int | `180` | 探针默认超时秒数（另端口启动 + 健康检查）。机器慢、插件多的时候调大；dev_probe 自己传了 timeout 就以传入值为准。 |

## 注意事项

- 探针超时默认 180 秒，机器慢或插件多要调大
- 单次写入上限 200KB，超了直接拒绝、不落盘
- 探针失败时 patch 会保留，可以人工看过再决定

## License

MIT
