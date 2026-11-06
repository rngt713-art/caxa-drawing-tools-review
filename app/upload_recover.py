"""Explicit recovery for operations whose remote result is unknown.

Nothing is replayed automatically.  An operator looks at CAXA, decides whether
the write happened, and records that decision for exactly one operation:

  python upload_recover.py list
  python upload_recover.py confirm --task T... --kind upload --key 01.01 \
      --remote-id 123456 --class-id 1002 --iterator 3
  python upload_recover.py no-write --task T... --kind upload --key 01.01 \
      --note "CAXA 中未找到该图纸，已确认未写入"
  python upload_recover.py release-lock --task T...

``confirm`` means "verified success" (IDs/type/iteration recorded as evidence);
``no-write`` means "verified that nothing was written", which allows a retry.
Only the addressed operation changes; other confirmed successes are kept.
"""

from __future__ import annotations

import argparse
import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import journal as journal_module  # noqa: E402


def _journal(args) -> journal_module.TaskJournal:
    return journal_module.TaskJournal(args.journal)


def cmd_list(args) -> int:
    with _journal(args) as journal:
        tasks = journal.list_tasks(limit=args.limit)
        found = False
        for task in tasks:
            pending = journal.pending_operations(task["task_id"])
            if not pending:
                continue
            found = True
            print(
                "任务 %s  状态=%s  阶段=%s  服务器=%s:%s  账号=%s"
                % (
                    task["task_id"],
                    task["status"],
                    task["stage"],
                    task["server"],
                    task["port"],
                    task["login_user"],
                )
            )
            for row in pending:
                print(
                    "  %-8s %-40s 阶段=%-18s 状态=%s 远端ID=%s 类型=%s 版次=%s"
                    % (
                        row["kind"],
                        row["key"],
                        row["phase"],
                        row["status"],
                        row["remote_id"] or "-",
                        row["class_id"] or row["class_name"] or "-",
                        row["iterator_no"] or "-",
                    )
                )
                if row.get("error"):
                    print("      错误：%s" % row["error"])
        if not found:
            print("没有待核实的操作。")
        return 0


def cmd_resolve(args, result: str) -> int:
    with _journal(args) as journal:
        try:
            record = journal.resolve_operation(
                args.task,
                args.kind,
                args.key,
                result=result,
                remote_id=args.remote_id,
                class_id=args.class_id,
                iterator_no=args.iterator or "",
                link_type=args.link_type or "",
                note=args.note or "",
            )
        except ValueError as exc:
            print("失败：%s" % exc)
            return 2
        print(
            "已记录：任务 %s %s/%s -> %s（远端ID=%s，类型=%s，版次=%s）"
            % (
                args.task,
                record["kind"],
                record["key"],
                record["status"],
                record["remote_id"] or "-",
                record["class_id"] or "-",
                record["iterator_no"] or "-",
            )
        )
        remaining = journal.pending_operations(args.task)
        if remaining:
            print("仍有 %d 项待核实，请继续处理。" % len(remaining))
        else:
            print("该任务已无待核实项；可再次上传/发流程（已成功的操作不会重做）。")
        return 0


def cmd_release(args) -> int:
    with _journal(args) as journal:
        ok = journal.release_lock(args.task)
        stale = journal.release_stale_locks()
        if ok:
            print("已释放任务锁。")
        else:
            print("没有找到该任务锁。")
        if stale:
            print("已清理 %d 个崩溃遗留锁（进程已不存在）。" % stale)
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="图纸上传待核实操作的人工恢复工具")
    parser.add_argument("--journal", default=None, help="日志数据库路径（默认本机用户目录）")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="列出待核实的任务与操作")
    listing.add_argument("--limit", type=int, default=50)
    listing.set_defaults(func=cmd_list)

    confirm = sub.add_parser("confirm", help="人工核实：远端写入已成功")
    confirm.add_argument("--task", required=True)
    confirm.add_argument("--kind", required=True, choices=("upload", "relation", "workflow"))
    confirm.add_argument("--key", required=True)
    confirm.add_argument("--remote-id", default=None)
    confirm.add_argument("--class-id", default=None)
    confirm.add_argument("--iterator", default="")
    confirm.add_argument("--link-type", default="")
    confirm.add_argument("--note", default="")
    confirm.set_defaults(func=lambda args: cmd_resolve(args, "success"))

    no_write = sub.add_parser("no-write", help="人工核实：确认没有写入，可安全重试")
    no_write.add_argument("--task", required=True)
    no_write.add_argument("--kind", required=True, choices=("upload", "relation", "workflow"))
    no_write.add_argument("--key", required=True)
    no_write.add_argument("--remote-id", default=None)
    no_write.add_argument("--class-id", default=None)
    no_write.add_argument("--iterator", default="")
    no_write.add_argument("--link-type", default="")
    no_write.add_argument("--note", default="")
    no_write.set_defaults(func=lambda args: cmd_resolve(args, "no_write"))

    release = sub.add_parser("release-lock", help="清理崩溃遗留的任务锁")
    release.add_argument("--task", required=True)
    release.set_defaults(func=cmd_release)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
