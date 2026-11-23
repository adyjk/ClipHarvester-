"""命令行入口。

用法：
  python main.py fetch         # 进货：低速拉一批元数据入库
  python main.py summarize     # 用本地模型给待摘要视频生成简介
  python main.py serve         # 打开本地网页界面
  python main.py search 关键词  # 命令行检索（纯本地，不联网）
  python main.py stats         # 看库内统计
"""

from __future__ import annotations

import argparse
import json
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from videofinder import config, ingest, scoring, store, web


def cmd_fetch(args) -> None:
    cfg = config.load()
    if args.limit:
        cfg["ingest"]["max_per_run"] = args.limit
    if args.interval:
        cfg["ingest"]["request_interval"] = args.interval
    ingest.run_fetch(cfg, log=print)


def cmd_summarize(args) -> None:
    cfg = config.load()
    if args.model:
        cfg["ai"]["model"] = args.model
    ingest.run_summarize(cfg, log=print, limit=args.n, force=args.force,
                         use_content=not args.no_content)


def cmd_fetch_douyin(args) -> None:
    from videofinder import douyin

    douyin.fetch_feed(config.load(), log=print, limit=args.n)


def cmd_fetch_zhihu(args) -> None:
    from videofinder import zhihu

    zhihu.fetch_hot(config.load(), log=print, limit=args.n)


def cmd_fetch_tieba(args) -> None:
    from videofinder import tieba

    tieba.fetch_posts(config.load(), log=print, limit=args.n)


def cmd_fetch_tap(args) -> None:
    from videofinder import tap

    tap.fetch_forum(config.load(), log=print, limit=args.n)


def cmd_digest(args) -> None:
    cfg = config.load()
    ingest.run_digest(cfg, log=print, limit=args.n, workers=args.workers,
                      model_name=args.model, force=args.force, source=args.source)


def cmd_transcribe(args) -> None:
    cfg = config.load()
    from videofinder import asr

    asr.run_transcribe(cfg, log=print, limit=args.n, workers=args.workers,
                       model_name=args.model)


def cmd_serve(args) -> None:
    cfg = config.load()
    if args.port:
        cfg["server"]["port"] = args.port
    web.serve(cfg)


def cmd_search(args) -> None:
    cfg = config.load()
    conn = store.connect(config.path(cfg, "db"))
    store.init_db(conn)
    rows = store.search(conn, q=" ".join(args.keywords), limit=args.n, sort=args.sort)
    if not rows:
        print("本地库里没有匹配结果")
        return
    for i, r in enumerate(rows, 1):
        try:
            s = json.loads(r["summary"]) if r["summary"] else {}
        except ValueError:
            s = {}
        print(f"{i:>3}. [{r['score']:.1f}] {r['title']}")
        print(f"     {r['author']} · {r['duration_s'] // 60}分 · {r['play']}播放 · {r['url']}")
        if s.get("one_line"):
            print(f"     {s['one_line']}")
    conn.close()


def cmd_stats(args) -> None:
    cfg = config.load()
    conn = store.connect(config.path(cfg, "db"))
    store.init_db(conn)
    print(json.dumps(store.stats(conn), ensure_ascii=False, indent=2))
    conn.close()


def cmd_rescore(args) -> None:
    cfg = config.load()
    conn = store.connect(config.path(cfg, "db"))
    store.init_db(conn)
    print(f"重算 {scoring.apply_all(conn, cfg)} 条分数")
    conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="本地视频筛选器：进货与检索分离")
    sub = p.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("fetch", help="拉取元数据入库")
    pf.add_argument("--limit", type=int, default=0, help="本次最多拉多少条")
    pf.add_argument("--interval", type=float, default=0.0, help="请求间隔秒数")
    pf.set_defaults(func=cmd_fetch)

    ps = sub.add_parser("summarize", help="生成 AI 简介")
    ps.add_argument("--n", type=int, default=0, help="本次摘要多少条")
    ps.add_argument("--force", action="store_true", help="连已生成的一起重做")
    ps.add_argument("--model", default="", help="临时指定本地模型名")
    ps.add_argument("--no-content", action="store_true", help="不抓弹幕/热评，只用元数据")
    ps.set_defaults(func=cmd_summarize)

    pdy = sub.add_parser("fetch-douyin", help="抖音推荐 feed（走真实浏览器，免逆签名）")
    pdy.add_argument("--n", type=int, default=40)
    pdy.set_defaults(func=cmd_fetch_douyin)

    pzh = sub.add_parser("fetch-zhihu", help="知乎热榜（匿名接口，不需要登录）")
    pzh.add_argument("--n", type=int, default=50)
    pzh.set_defaults(func=cmd_fetch_zhihu)

    ptb = sub.add_parser("fetch-tieba", help="贴吧帖子（借调试浏览器，免登录）")
    ptb.add_argument("--n", type=int, default=10)
    ptb.set_defaults(func=cmd_fetch_tieba)

    ptap = sub.add_parser("fetch-tap", help="TapTap 论坛帖子（免登录）")
    ptap.add_argument("--n", type=int, default=40)
    ptap.set_defaults(func=cmd_fetch_tap)

    pd = sub.add_parser("digest", help="内容级总结：转写语音 → AI 总结（并行）")
    pd.add_argument("--n", type=int, default=20)
    pd.add_argument("--workers", type=int, default=3)
    pd.add_argument("--model", default="small", choices=["small", "medium", "large-v3"])
    pd.add_argument("--force", action="store_true", help="连已总结过的也按新规范重做")
    pd.add_argument("--source", default="", help="只处理某个平台，如 douyin")
    pd.set_defaults(func=cmd_digest)

    pt = sub.add_parser("transcribe", help="并行语音转写（下音频→whisper）")
    pt.add_argument("--n", type=int, default=20)
    pt.add_argument("--workers", type=int, default=3)
    pt.add_argument("--model", default="medium", choices=["small", "medium", "large-v3"])
    pt.set_defaults(func=cmd_transcribe)

    pv = sub.add_parser("serve", help="启动本地网页界面")
    pv.add_argument("--port", type=int, default=0)
    pv.set_defaults(func=cmd_serve)

    pq = sub.add_parser("search", help="本地检索")
    pq.add_argument("keywords", nargs="+")
    pq.add_argument("--n", type=int, default=20)
    pq.add_argument("--sort", default="score", choices=["score", "new", "play"])
    pq.set_defaults(func=cmd_search)

    pst = sub.add_parser("stats", help="库内统计")
    pst.set_defaults(func=cmd_stats)

    pr = sub.add_parser("rescore", help="重算全部分数")
    pr.set_defaults(func=cmd_rescore)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
