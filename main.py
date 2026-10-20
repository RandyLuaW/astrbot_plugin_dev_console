# -*- coding: utf-8 -*-
"""开发工位：让 Agent 在沙箱里改代码，经旁观进程验证后才落地。

为什么不是直接给 shell：
    改的是自己正在跑的进程。改 .py 不热重载，重启崩了就再也起不来，
    连"报告失败"的能力都没有。所以写入与落地必须分离，中间隔一个活着
    的旁观进程（探针实例）。

工具链：
    dev_read_file    读源码（只读）
    dev_write_file   写候选（只能写 staging/）
    dev_list_staging 看候选清单
    dev_probe        跑探针（compileall + 另端口启动 + 健康检查 + 出 patch）
    dev_apply        落地（要求上次探针 PASS 且候选未变）
    dev_status       看工位状态
    dev_journal      追加工程日志

约束：
    - 全部工具仅管理员可用。
    - 候选路径必须在 astrbot/ 或 main.py 下。
    - 落地不做二次验证，靠指纹保证"验过的就是落地的"。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_dev_console"

PLUGIN_DIR = Path(__file__).resolve().parent
SRC = PLUGIN_DIR.parents[2]                 # AstrBot/
QQBOT = SRC.parent                          # QQBot/
SANDBOX = SRC / "data" / "dev-sandbox"
STAGING = SANDBOX / "staging"
JOURNAL = SANDBOX / "JOURNAL.md"
STATE = SANDBOX / "state.json"
PROBE = QQBOT / "tools" / "probe_restart.py"
PY = SRC / ".venv" / "Scripts" / "python.exe"

ALLOWED_PREFIX = ("astrbot/", "main.py")
MAX_WRITE_BYTES = 200 * 1024
MAX_READ_LINES = 600
PROBE_TIMEOUT = 180

DEFAULT_CONFIG = {
    "enabled": True,
    "inject_hint": True,
    "max_read_lines": MAX_READ_LINES,
    "max_write_bytes": MAX_WRITE_BYTES,
    "probe_timeout": PROBE_TIMEOUT,
}

# 不当作候选的垃圾文件（她自己踩过：staging 里跑 py_compile 会留下 .pyc）
IGNORED_DIRS = {"__pycache__", ".git", ".pytest_cache"}
IGNORED_SUFFIX = (".pyc", ".pyo", ".tmp", ".log")
IGNORED_NAMES = {".DS_Store", "Thumbs.db"}


def _admin(event: AstrMessageEvent) -> bool:
    try:
        return event.is_admin() is True
    except Exception:
        return False


def _dev_session(event: AstrMessageEvent) -> bool:
    """开发会话 = 管理员 + 私聊。群里一律不给，避免公开暴露内部路径与步数。"""
    if not _admin(event):
        return False
    try:
        return event.is_private_chat() is True
    except Exception:
        return False


DEV_HINT = (
    "<dev_console>\n"
    "你现在在自己的工位上，可以改自己的代码。流程固定五步：\n"
    "dev_read_file 读源码 → dev_write_file 写候选（只会进沙箱）→ dev_probe 验证（约 30 秒）"
    "→ dev_apply 落地 → dev_journal 记一笔。\n"
    "探针没 PASS 就不许落地。落地后要提醒主人重启才生效。"
    "不确定改什么就先 dev_status 看一眼。\n"
    "</dev_console>"
)


def _rel_ok(rel: str) -> bool:
    if not rel:
        return False
    if rel.startswith(("/", "\\")) or ".." in Path(rel).parts:
        return False
    return rel.replace("\\", "/").startswith(ALLOWED_PREFIX)


def _fingerprint() -> str:
    h = hashlib.sha256()
    if STAGING.exists():
        for p in sorted(STAGING.rglob("*")):
            if p.is_file():
                h.update(p.relative_to(STAGING).as_posix().encode("utf-8"))
                h.update(p.read_bytes())
    return h.hexdigest()[:16]


def _staged() -> list[Path]:
    if not STAGING.exists():
        return []
    out: list[Path] = []
    for p in sorted(STAGING.rglob("*")):
        if not p.is_file():
            continue
        if p.name in IGNORED_NAMES or p.name.endswith(IGNORED_SUFFIX):
            continue
        if any(part in IGNORED_DIRS for part in p.parts):
            continue
        out.append(p)
    return out


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(data: dict) -> None:
    SANDBOX.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _clear_staging(items: list[Path]) -> int:
    """落地后清空候选，避免下次探针把旧文件又验一遍。"""
    removed = 0
    for p in items:
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    for d in sorted(STAGING.rglob("*"), key=lambda x: len(x.parts), reverse=True):
        if d.is_dir() and d != STAGING:
            try:
                d.rmdir()
            except OSError:
                pass
    return removed


def _run_probe(timeout: int) -> tuple[int, str, float]:
    cmd = [str(PY), str(PROBE), "--timeout", str(timeout)]
    t0 = time.time()
    r = subprocess.run(
        cmd,
        cwd=str(QQBOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout + 150,
    )
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode, out, time.time() - t0


def _tail(text: str, n: int = 18) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


class DevConsolePlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = dict(DEFAULT_CONFIG)
        if isinstance(config, dict):
            self.config.update({k: v for k, v in config.items() if k in DEFAULT_CONFIG})
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        logger.info(
            f"[dev-console] 工位就绪：{SANDBOX}（enabled={self._cfg('enabled')}）"
        )

    async def terminate(self) -> None:
        pass

    # ------------------------------------------------------------ 配置

    def _cfg(self, key: str) -> Any:
        value = self.config.get(key, DEFAULT_CONFIG.get(key))
        if value is None:
            return DEFAULT_CONFIG.get(key)
        return value

    def _int_cfg(self, key: str, low: int, high: int) -> int:
        """配置值容错：非数字/越界一律夹到合法区间。"""
        try:
            value = int(self._cfg(key))
        except (TypeError, ValueError):
            value = int(DEFAULT_CONFIG.get(key) or low)
        return max(low, min(value, high))

    def _gate(self, event: AstrMessageEvent) -> str | None:
        """统一入口：管理员私聊 + 总开关。返回 None 表示放行。"""
        if not _dev_session(event):
            return "无权限：开发工位只在管理员私聊里可用。"
        if not self._cfg("enabled"):
            return "开发工位已关闭（WebUI 插件配置里把「启用开发工位」打开）。"
        return None

    # ------------------------------------------------------------ 钩子

    @filter.on_llm_request(priority=-900)
    async def inject_dev_hint(self, event: AstrMessageEvent, req: Any) -> None:
        """只在管理员私聊里告诉她自己有工位；群里完全不出现。"""
        if not _dev_session(event) or not self._cfg("enabled"):
            return
        if not self._cfg("inject_hint"):
            return
        try:
            from astrbot.core.agent.message import TextPart

            part = TextPart(text=DEV_HINT)
            if hasattr(part, "mark_as_temp"):
                part = part.mark_as_temp()
            req.extra_user_content_parts.append(part)
        except Exception as exc:
            logger.warning(f"[dev-console] 提示注入失败（不影响主流程）：{exc}")

    # ------------------------------------------------------------ 工具

    @filter.llm_tool(name="dev_read_file")
    async def dev_read_file(
        self,
        event: AstrMessageEvent,
        path: str,
        max_lines: float = 200,
        offset: float = 1,
    ) -> str:
        """读取 AstrBot 源码文件。路径相对 AstrBot 根目录，例如 astrbot/core/pipeline/result_decorate/stage.py。只能读 astrbot/ 与 main.py，写不了任何东西。改代码前先读，别凭记忆猜。文件很长时用 offset 分段读。

        Args:
            path(string): 源码相对路径，必须以 astrbot/ 开头
            max_lines(number): 最多返回多少行，默认 200，上限 600
            offset(number): 从第几行开始读，默认 1。返回内容里带行号，翻页时把上次最后一行的行号加一填进来
        """
        err = self._gate(event)
        if err:
            return err
        rel = str(path).strip().replace("\\", "/")
        if not _rel_ok(rel):
            return "路径不合法：只能读 astrbot/ 或 main.py 下的文件。"
        target = (SRC / rel).resolve()
        if not str(target).startswith(str(SRC.resolve())):
            return "路径越界。"
        if not target.is_file():
            return f"文件不存在：{rel}"
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as exc:
            return f"读取失败：{exc}"
        total = len(lines)
        limit = max(1, min(int(max_lines or 200), self._int_cfg("max_read_lines", 50, 5000)))
        start = max(1, int(offset or 1))
        if start > total:
            return f"offset {start} 超出文件长度（共 {total} 行）。"
        chunk = lines[start - 1 : start - 1 + limit]
        body = "\n".join(
            f"{i:>5}| {ln}" for i, ln in enumerate(chunk, start=start)
        )
        end = start + len(chunk) - 1
        tail_note = ""
        if end < total:
            tail_note = f"\n...(共 {total} 行，本次 {start}-{end}，继续读用 offset={end + 1})"
        return f"{rel}（{total} 行）\n{body}{tail_note}"

    @filter.llm_tool(name="dev_write_file")
    async def dev_write_file(
        self, event: AstrMessageEvent, path: str, content: str
    ) -> str:
        """把候选代码写入沙箱 staging/，不会碰正在运行的源码。路径相对 AstrBot 根目录，例如 astrbot/core/foo.py。写完必须调用 dev_probe 验证，验证通过再用 dev_apply 落地。一次写一个完整文件，不要写半截。

        Args:
            path(string): 目标相对路径，必须以 astrbot/ 或 main.py 开头
            content(string): 完整文件内容，不要用省略号代替已有代码
        """
        err = self._gate(event)
        if err:
            return err
        rel = str(path).strip().replace("\\", "/")
        if not _rel_ok(rel):
            return "路径不合法：只能写 astrbot/ 或 main.py 下的文件。"
        data = str(content or "")
        if not data.strip():
            return "内容为空，拒绝写入。"
        raw = data.encode("utf-8")
        max_bytes = self._int_cfg("max_write_bytes", 1024, 5 * 1024 * 1024)
        if len(raw) > max_bytes:
            return f"内容过大：{len(raw)//1024}KB，上限 {max_bytes//1024}KB。"
        target = (STAGING / rel).resolve()
        if not str(target).startswith(str(STAGING.resolve())):
            return "路径越界。"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data, encoding="utf-8")
        return (
            f"已写入 staging/{rel}（{len(raw)//1024}KB，{len(data.splitlines())} 行）\n"
            f"当前候选指纹 {_fingerprint()}，共 {len(_staged())} 个文件\n"
            "下一步：dev_probe 验证"
        )

    @filter.llm_tool(name="dev_list_staging")
    async def dev_list_staging(self, event: AstrMessageEvent) -> str:
        """列出当前沙箱里待验证的候选文件及其大小。不确定改过什么时先看这个。"""
        err = self._gate(event)
        if err:
            return err
        items = _staged()
        if not items:
            return "staging 为空。"
        lines = []
        for p in items:
            rel = p.relative_to(STAGING).as_posix()
            kb = max(1, p.stat().st_size // 1024)
            lines.append(f"- {rel} ({kb}KB)")
        return f"候选 {len(items)} 个，指纹 {_fingerprint()}：\n" + "\n".join(lines)

    @filter.llm_tool(name="dev_probe")
    async def dev_probe(self, event: AstrMessageEvent, timeout: float = 180) -> str:
        """在隔离副本里验证候选：静态编译 → 另端口启动探针实例 → 健康检查 → 生成补丁。不碰主实例，失败也不会影响线上。改完代码必须调用本工具，看到 PASS 才能申请落地。

        Args:
            timeout(number): 探针启动超时秒数，默认 180
        """
        err = self._gate(event)
        if err:
            return err
        if not _staged():
            return "staging 为空：没有候选可验。先用 dev_write_file 写入改动。"
        if not PROBE.is_file():
            return f"探针脚本不存在：{PROBE}"

        tmo = int(timeout or self._int_cfg("probe_timeout", 30, 1800))
        async with self._lock:
            try:
                code, out, cost = await asyncio.to_thread(_run_probe, tmo)
            except subprocess.TimeoutExpired:
                return f"探针超时（{tmo}s + 缓冲），已放弃。检查 staging 里是否有死循环。"
            except Exception as exc:
                return f"探针启动异常：{exc}"

        ok = code == 0 and "[PASS]" in out
        _save_state(
            {
                "ok": ok,
                "fingerprint": _fingerprint(),
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "cost": round(cost, 1),
                "files": [p.relative_to(STAGING).as_posix() for p in _staged()],
                "tail": _tail(out, 12),
            }
        )
        head = "PASS" if ok else "FAIL"
        body = _tail(out, 14)
        hint = (
            "可以调用 dev_apply 落地。"
            if ok
            else "未通过，源码未动。修好 staging 后重跑 dev_probe。"
        )
        return f"[{head}] 探针耗时 {cost:.1f}s\n{body}\n{hint}"

    @filter.llm_tool(name="dev_apply")
    async def dev_apply(self, event: AstrMessageEvent) -> str:
        """把已验证通过的候选落到真实源码。前提：上次 dev_probe 结果为 PASS，且候选文件在验证后没被改动过（指纹一致）。落地后需要人工重启 AstrBot 才生效，本工具不会重启。"""
        err = self._gate(event)
        if err:
            return err
        items = _staged()
        if not items:
            return "staging 为空，没有可落地的内容。"
        st = _load_state()
        if not st.get("ok"):
            return "上次探针未通过或还没跑过，拒绝落地。先 dev_probe。"
        if st.get("fingerprint") != _fingerprint():
            return (
                "候选在验证之后被改动过，指纹不一致，拒绝落地。"
                "重新跑一次 dev_probe。"
            )

        written = []
        for p in items:
            rel = p.relative_to(STAGING).as_posix()
            target = (SRC / rel).resolve()
            if not str(target).startswith(str(SRC.resolve())):
                return f"路径越界，已中止：{rel}"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(p.read_bytes())
            written.append(rel)

        cleared = _clear_staging(items)
        _save_state({"ok": False, "fingerprint": _fingerprint(), "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
        logger.info(f"[dev-console] 落地 {len(written)} 个文件：{written}")
        return (
            f"已落地 {len(written)} 个文件：\n- " + "\n- ".join(written) +
            f"\n候选已清空 {cleared} 个，补丁留在 data/dev-sandbox/patches/。"
            "\n主实例需重启才生效；重启前别在 WebUI 点保存。"
        )

    @filter.llm_tool(name="dev_status")
    async def dev_status(self, event: AstrMessageEvent) -> str:
        """查看开发工位状态：候选文件、上次探针结果、源码改动数。开工前和收工前各看一次。"""
        err = self._gate(event)
        if err:
            return err
        st = _load_state()
        items = _staged()
        git = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(SRC),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        dirty = len([ln for ln in (git.stdout or "").splitlines() if ln.strip()])
        lines = [
            f"候选文件：{len(items)} 个（指纹 {_fingerprint()}）",
        ]
        for p in items[:8]:
            lines.append(f"  - {p.relative_to(STAGING).as_posix()}")
        if st:
            lines.append(
                f"上次探针：{'PASS' if st.get('ok') else 'FAIL'} @ {st.get('ts')}"
                f"，{st.get('cost')}s，指纹 {st.get('fingerprint')}"
            )
            if st.get("tail"):
                lines.append("日志尾：" + st["tail"].replace("\n", " / ")[-400:])
        else:
            lines.append("上次探针：从未运行")
        lines.append(f"源码工作区改动：{dirty} 个文件")
        return "\n".join(lines)

    @filter.llm_tool(name="dev_journal")
    async def dev_journal(self, event: AstrMessageEvent, entry: str) -> str:
        """追加一条工程日志到 data/dev-sandbox/JOURNAL.md。每次改完代码都要写：改了什么、为什么、下次别踩什么坑。只追加不覆盖。

        Args:
            entry(string): 一段简短记录，建议包含「动了/为什么/坑/结果」四要素
        """
        err = self._gate(event)
        if err:
            return err
        text = str(entry or "").strip()
        if not text:
            return "内容为空。"
        items = [p.relative_to(STAGING).as_posix() for p in _staged()]
        stamp = time.strftime("%Y-%m-%d %H:%M")
        block = f"\n### {stamp} | {text.splitlines()[0][:60]}\n"
        if len(text.splitlines()) > 1:
            block += "\n".join(text.splitlines()[1:]) + "\n"
        if items:
            block += "- 动了：" + "、".join(items[:6]) + "\n"
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        with JOURNAL.open("a", encoding="utf-8") as fh:
            fh.write(block)
        return f"已追加日志（{stamp}）。"

    # ------------------------------------------------------------ 指令

    @filter.command("dev")
    async def dev_cmd(self, event: AstrMessageEvent):
        """开发工位指令：/dev status | /dev probe | /dev apply | /dev clean"""
        if not _dev_session(event):
            yield event.plain_result("仅管理员可用。")
            return
        if not self._cfg("enabled"):
            yield event.plain_result(
                "开发工位已关闭（WebUI 插件配置里把「启用开发工位」打开）。"
            )
            return
        arg = (event.message_str or "").replace("/dev", "", 1).strip().lower()

        if arg in ("", "status"):
            st = _load_state()
            items = _staged()
            head = f"候选 {len(items)} 个 | 指纹 {_fingerprint()}"
            last = (
                f"上次探针 {'PASS' if st.get('ok') else 'FAIL'} @ {st.get('ts')}"
                if st
                else "上次探针 从未运行"
            )
            yield event.plain_result(f"{head}\n{last}")
            return

        if arg == "probe":
            if not _staged():
                yield event.plain_result("staging 为空。")
                return
            async with self._lock:
                code, out, cost = await asyncio.to_thread(
                    _run_probe, self._int_cfg("probe_timeout", 30, 1800)
                )
            ok = code == 0 and "[PASS]" in out
            _save_state(
                {
                    "ok": ok,
                    "fingerprint": _fingerprint(),
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "cost": round(cost, 1),
                    "files": [p.relative_to(STAGING).as_posix() for p in _staged()],
                    "tail": _tail(out, 12),
                }
            )
            yield event.plain_result(
                f"[{'PASS' if ok else 'FAIL'}] {cost:.1f}s\n{_tail(out, 12)}"
            )
            return

        if arg == "apply":
            st = _load_state()
            if not st.get("ok") or st.get("fingerprint") != _fingerprint():
                yield event.plain_result("未通过验证或候选已变更，拒绝落地。")
                return
            written = []
            items = _staged()
            for p in items:
                rel = p.relative_to(STAGING).as_posix()
                (SRC / rel).parent.mkdir(parents=True, exist_ok=True)
                (SRC / rel).write_bytes(p.read_bytes())
                written.append(rel)
            _clear_staging(items)
            _save_state(
                {"ok": False, "fingerprint": _fingerprint(),
                 "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
            )
            yield event.plain_result(
                f"已落地 {len(written)} 个文件，候选已清空，重启后生效。"
            )
            return

        if arg == "clean":
            code, out, _ = await asyncio.to_thread(
                lambda: (
                    subprocess.run(
                        [str(PY), str(PROBE), "--clean"],
                        cwd=str(QQBOT),
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    ).returncode,
                    "",
                    0,
                )
            )
            yield event.plain_result(f"清理完成（exit={code}）。")
            return

        yield event.plain_result("用法：/dev status | probe | apply | clean")
