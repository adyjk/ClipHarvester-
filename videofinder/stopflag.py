"""全局「结束当前任务」开关：界面点按钮置位，各流程循环里检查后提前收工。"""
import threading

EVENT = threading.Event()


def reset() -> None:
    EVENT.clear()


def request() -> None:
    EVENT.set()


def stopped() -> bool:
    return EVENT.is_set()
