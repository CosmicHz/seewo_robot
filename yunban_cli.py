# -*- coding: utf-8 -*-
"""
云班（yunban）客户端命令行封装 —— 方便调用 / 测试云班各功能，含考勤。

复用 yunban_token.yunban_client（真实登录会话推导的 yunban 实例），所有功能经子命令驱动。
yunban.py 现在返回数据模型（YunbanClass 等），本脚本按属性访问。

用法:
  python yunban_cli.py classes                         # 班级列表
  python yunban_cli.py student <classUid>              # 某班学生列表
  python yunban_cli.py events <roomUid>                # 某教室考勤事件
  python yunban_cli.py eventtime <eventIndex>          # 解析第 N 个考勤事件的起止时间
  python yunban_cli.py attend <班级名> [eventId]        # 给指定班全班签到（考勤，eventId 缺省第一个事件）
  python yunban_cli.py roster                          # 列出所有班级 + 各班人数（便于定位班级/教室）
"""

import sys

from yunban_token import yunban_client


def _cls_events(room_uid):
    """获取某教室考勤事件（带容错）"""
    try:
        return yunban_client.getevents(room_uid)
    except Exception as e:
        print(f"[ERROR] 获取考勤事件失败: {e}")
        return []


def cmd_classes():
    """班级列表"""
    for cl in yunban_client.getclasslist():
        print(f"{cl.uid}  {cl.name:<10} roomUid={cl.roomUid}")


def cmd_students(class_uid):
    """某班学生列表"""
    try:
        students = yunban_client.getstulist(class_uid)
    except Exception as e:
        print(f"[ERROR] 获取学生列表失败: {e}")
        return
    for s in students:
        print(f"{s.uid}  {s.name:<6} sid={s.sid}")


def cmd_events(room_uid):
    """某教室考勤事件"""
    events = _cls_events(room_uid)
    if not events:
        print("[INFO] 无考勤事件")
        return
    for i, e in enumerate(events):
        print(f"[{i}] eventId={e.eventId}  {e.name}  endTime={e.endTime}")


def cmd_eventtime(event_index):
    """解析第 N 个考勤事件的起止时间（需要一个班级的 roomUid）"""
    classes = yunban_client.getclasslist()
    room = next((c.roomUid for c in classes if c.roomUid), classes[0].roomUid)
    events = _cls_events(room)
    if not events:
        print("[INFO] 无考勤事件可供解析")
        return
    try:
        event = events[int(event_index)]
    except (IndexError, ValueError):
        print(f"[ERROR] 事件索引无效: {event_index}")
        return
    start, end = yunban_client.geteventtime(event)
    print(f"eventName={event.name}")
    print(f"randomtime 候选区间 [{start} ~ {event.endTime}]")
    print(f"随机签到时间示例: {yunban_client.randomtime(event)}")


def _match_class(class_key):
    """按班级 uid 精确定位，或按名称子串匹配，返回唯一班或 None（含提示）"""
    classes = yunban_client.getclasslist()
    matches = [c for c in classes if c.uid == class_key or class_key in c.name]
    if not matches:
        print(f"[ERROR] 未找到班级: {class_key}")
        return None
    if len(matches) > 1:
        print(f"[ERROR] 班级名称 '{class_key}' 匹配到多个，请更精确：")
        for c in matches:
            print(f"   {c.uid}  {c.name}  roomUid={c.roomUid}")
        return None
    return matches[0]


def _match_event(events, event_key):
    """按 eventId/事件名定位考勤事件；缺省取第一个"""
    if not event_key:
        return events[0]
    for e in events:
        if e.eventId == event_key or e.name == event_key:
            return e
    print(f"[ERROR] 未找到考勤事件: {event_key}，可选：")
    for e in events:
        print(f"   {e.eventId}  {e.name}")
    return None


def cmd_attend(class_key, event_key=""):
    """给指定班全班签到（考勤）"""
    target = _match_class(class_key)
    if not target:
        return

    name, room_uid = target.name, target.roomUid
    print(f"目标班级: {name}  roomUid={room_uid}")

    events = _cls_events(room_uid)
    if not events:
        print("[ERROR] 无考勤事件，无法签到")
        return
    event = _match_event(events, event_key)
    if not event:
        return

    students = yunban_client.getstulist(target.uid)
    if not students:
        print("[ERROR] 班级无学生，无法签到")
        return

    import time as _time

    date_now = _time.strftime("%Y-%m-%d", _time.localtime())
    for s in students:
        fake_time = yunban_client.randomtime(event)
        print(
            f"[ATTEND] {s.name} 随机时间 {date_now} {fake_time} -> "
            f"{yunban_client.attend(s.name, s.uid, s.sid, event, date_now, fake_time, target.uid, room_uid)}"
        )


def cmd_roster():
    """列出所有班级 + 各班人数，便于定位班级/教室"""
    for i, cl in enumerate(yunban_client.getclasslist()):
        try:
            n = len(yunban_client.getstulist(cl.uid))
        except Exception:
            n = -1
        print(f"[{i}] {cl.name:<12} roomUid={cl.roomUid:<34} 学生数={n}")


COMMANDS = {
    "classes": cmd_classes,
    "student": cmd_students,
    "events": cmd_events,
    "eventtime": cmd_eventtime,
    "attend": cmd_attend,
    "roster": cmd_roster,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    args = sys.argv[2:]
    try:
        if cmd == "classes":
            cmd_classes()
        elif cmd == "student":
            cmd_students(args[0])
        elif cmd == "events":
            cmd_events(args[0])
        elif cmd == "eventtime":
            cmd_eventtime(args[0] if args else "0")
        elif cmd == "attend":
            cmd_attend(args[0], args[1] if len(args) > 1 else "")
        elif cmd == "roster":
            cmd_roster()
    except (IndexError, ValueError) as e:
        print(f"[ERROR] 参数不足或无效: {e}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
