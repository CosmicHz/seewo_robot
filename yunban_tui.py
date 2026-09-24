# -*- coding: utf-8 -*-
"""
云班（yunban）TUI —— 三列浏览器 + 选择 + 按钮操作，含考勤与进度条

基于 Textual，复用 yunban_token.yunban_client 的真实会话。布局：
  左列  班级列表
  中列  上方学生列表 / 下方考勤事件列表
  右列  最近一次接口的希沃原始返回

选中班级后，底部出现操作句：
  「获取 <班级> 的 [学生列表] [考勤列表]」—— 学生/事件已缓存时对应按钮变灰
  「为 <班级或学生> [考勤]」—— 需 班级+事件 或 学生+事件 同时选中才可点，批量考勤带进度条
输入框可作为按名称快速选中班级/学生的通道。
"""

import asyncio
import json
import os

import requests
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widgets._option_list import Option
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    OptionList,
    ProgressBar,
    Static,
)

from init import project_path, verify
from yunban import _get_yunban_base
from yunban_token import yunban_client
from models import YunbanEvent

BASE = _get_yunban_base()
HEADERS = yunban_client.headers
CACHE: dict[tuple, dict] = {}  # key=(method, url) -> 希沃原始返回
CACHE_FILE = project_path("yunban_cache.json")


def _load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            raw = json.loads(open(CACHE_FILE, encoding="utf-8").read())
            for method, url, data in raw:
                CACHE[(method, url)] = data
        except Exception:
            pass


def _save_cache():
    try:
        data = [[m, u, d] for (m, u), d in CACHE.items()]
        open(CACHE_FILE, "w", encoding="utf-8").write(json.dumps(data))
    except Exception:
        pass


_load_cache()

URL_CLASSES = f"{BASE}/api/classmember/v1/school/{yunban_client.schoolid}/classes"


def _students_url(class_uid):
    return f"{BASE}/api/classmember/v1/school/{yunban_client.schoolid}/students?classUids={class_uid}"


def _events_url(room_uid):
    return (
        f"{BASE}/api/attendance/v3/{yunban_client.schoolid}/events?roomUid={room_uid}"
    )


def _parents_url(child_uid):
    return f"{BASE}/api/kidnote/v1/{child_uid}/parent/note/count"


def _post_attend(payload: dict) -> dict:
    resp = requests.post(
        f"{BASE}/api/attendance/v1/{yunban_client.schoolid}/data",
        json=payload,
        headers=HEADERS,
        verify=verify,
    )
    return resp.json()


def fetch(url: str) -> tuple[dict, bool]:
    """GET 希沃。返回 (数据, 是否真正走了网络)。

    命中缓存时直接用、不请求；未命中时网络拉取并写缓存。
    第三列只忠实地反映真实网络请求的原始响应，故需要此标识。
    """
    key = ("GET", url)
    if key not in CACHE:
        CACHE[key] = requests.get(url, headers=HEADERS, verify=verify).json()
        _save_cache()
        return CACHE[key], True
    return CACHE[key], False


def _describe_event(e) -> str:
    """把考勤事件的所有字段整理成可读的多行文本"""
    lines = []
    lines.append(f"事件：{e.get('name', '')}")
    lines.append(
        f"  eventId={e.get('eventId', '')} · 版本 {e.get('eventVersion', '')} · 类型 {e.get('memberType', '')}"
    )
    lines.append(
        f"  时间：{e.get('startTime', '')} ~ {e.get('endTime', '')}　迟到线:{e.get('overTime', '')}　容差:{e.get('delayMinute', '')}分钟"
    )
    lines.append(
        f"  周期: cycleType={e.get('cycleType', '')}({e.get('cycleContent', '')})\n  userType={e.get('userType', '')} · attendanceType={e.get('attendanceType', '')}"
    )
    cls_list = e.get("classes") or []
    if cls_list:
        lines.append("  班级: " + ", ".join(c.get("className", "") for c in cls_list))
    cfg = e.get("config") or ""
    if cfg:
        try:
            c = json.loads(cfg)
            bp = c.get("banPaiConfig", {})
            if bp:
                lines.append(
                    f"  班牌时段: {bp.get('topStartTime', '')} ~ {bp.get('topEndTime', '')}"
                )
        except Exception:
            lines.append(f"  config={cfg}")
    for k, v in e.items():
        if k in (
            "name",
            "eventId",
            "eventVersion",
            "memberType",
            "startTime",
            "endTime",
            "overTime",
            "delayMinute",
            "cycleType",
            "cycleContent",
            "userType",
            "attendanceType",
            "classes",
            "config",
        ):
            continue
        if isinstance(v, (list, dict)):
            lines.append(f"  {k}={json.dumps(v, ensure_ascii=False)}")
        else:
            lines.append(f"  {k}={v}")
    return "\n".join(lines)


def _describe_student(s) -> str:
    """把学生字段整理成可读的多行文本（字段并非人人齐全，全部用 get 容错）"""
    lines = []
    lines.append(f"学生：{s.get('name', '')}")
    lines.append(f"  sid={s.get('sid', '')} · uid={s.get('uid', '')}")
    gender = s.get("gender")
    gender_txt = {0: "未知", 1: "男", 2: "女"}.get(gender, gender)
    lines.append(f"  性别: {gender_txt} · classUid={s.get('classUid', '')}")
    cards = s.get("extendCardIds")
    if cards:
        lines.append("  卡号: " + ", ".join(cards))
    for k in ("headImageUrl", "ucPassword"):
        v = s.get(k)
        if v not in (None, ""):
            lines.append(f"  {k}={v}")
    for k, v in s.items():
        if k in (
            "name",
            "sid",
            "uid",
            "gender",
            "classUid",
            "extendCardIds",
            "headImageUrl",
            "ucPassword",
        ):
            continue
        if isinstance(v, (list, dict)):
            v = json.dumps(v, ensure_ascii=False)
        lines.append(f"  {k}={v}")
    return "\n".join(lines)


def _describe_parent(p) -> str:
    """把家长字段整理成可读的多行文本"""
    lines = []
    lines.append(f"家长：{p.get('parentName', '')}")
    lines.append(
        f"  关系 #{p.get('parentShowIndex', '')} · 手机 {p.get('parentPhone', '')}"
    )
    lines.append(f"  parentUserUid={p.get('parentUserUid', '')}")
    lines.append(
        f"  {'已绑' if p.get('bindWx') else '未绑'}微信 · 未读 {p.get('notReadNoteCount', 0)} 条"
    )
    if p.get("tipsMessage"):
        lines.append(f"  提示: {p['tipsMessage']}")
    if p.get("parentHeadImage"):
        lines.append(f"  头像: {p['parentHeadImage']}")
    for k, v in p.items():
        if k in (
            "parentName",
            "parentShowIndex",
            "parentPhone",
            "parentUserUid",
            "bindWx",
            "notReadNoteCount",
            "tipsMessage",
            "parentHeadImage",
        ):
            continue
        if isinstance(v, (list, dict)):
            v = json.dumps(v, ensure_ascii=False)
        lines.append(f"  {k}={v}")
    return "\n".join(lines)


class YunbanTUI(App):
    """云班 TUI 应用"""

    CSS = """
    Screen { layout: vertical; }
    #panes { layout: horizontal; height: 1fr; }
    #cls_pane   { width: 30%; border: solid $primary;   padding: 1; }
    #mid_pane   { width: 40%; layout: vertical; }
    #stu_pane { border: solid $secondary; height: 1fr; padding: 1; }
    #bottom_pane { height: 1fr; layout: horizontal; }
    #evt_cell, #par_cell { border: solid $secondary; height: 1fr; padding: 1; }
    #raw_pane   { width: 30%; border: solid $warning; color: $secondary; }
    #cls_pane VerticalScroll, #mid_pane VerticalScroll, #raw_pane VerticalScroll {
        overflow-y: scroll; scrollbar-size-vertical: 1; }
    #cls_pane Static, #mid_pane Static, #raw_pane Static { width: 100%; }
    #incmd { width: 1fr; margin: 0 1; }
    #info_scroll { height: 10; padding: 0 2; border: none; }
    #info  { color: $text; }
    #actions { height: auto; layout: vertical; padding: 1 0 1 0; border: none; }
    #act1, #act2, #act3 { height: auto; layout: horizontal; align: left middle; padding: 0 2; }
    #act2 { margin-top: 1; }
    #act3 { margin-top: 1; }
    #act1 Static, #act2 Static, #act3 Static { width: auto; }
    #cls_name, #target_name, #par_target { color: $accent; max-width: 20; overflow: hidden; }
    #act1 Button, #act2 Button, #act3 Button { height: 1; border: none; padding: 0 1; margin: 0 0; }
    ProgressBar { width: 30; height: 1; display: none; }
    """

    BINDINGS = [("q", "quit", "退出"), ("r", "invalidate_cache", "清缓存并重拉")]

    def __init__(self) -> None:
        super().__init__()
        self.class_items: list = []
        self.students: list = []
        self.events: list = []
        self.parents: list = []
        self.sel_class = None
        self.sel_student = None
        self.sel_event = None
        self.sel_parent = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            VerticalScroll(OptionList(id="cls_list"), id="cls_pane"),
            VerticalScroll(
                Container(OptionList(id="stu_list"), id="stu_pane"),
                Container(
                    Container(OptionList(id="evt_list"), id="evt_cell"),
                    Container(OptionList(id="par_list"), id="par_cell"),
                    id="bottom_pane",
                ),
                id="mid_pane",
            ),
            VerticalScroll(Static("", id="raw_text"), id="raw_pane"),
            id="panes",
        )
        yield Input(placeholder="输入班级/学生名快速选中...", id="incmd")
        yield VerticalScroll(Static("", id="info"), id="info_scroll")
        yield Vertical(
            Horizontal(
                Static("获取 "),
                Static("____", id="cls_name"),
                Static(" 的 "),
                Button("学生列表", id="btn_stu", disabled=True),
                Button("考勤列表", id="btn_evt", disabled=True),
                id="act1",
            ),
            Horizontal(
                Static("为 "),
                Static("____", id="target_name"),
                Static(" "),
                Button("考勤", id="btn_attend", disabled=True),
                ProgressBar(id="pb", show_bar=True),
                id="act2",
            ),
            Horizontal(
                Static("获取 "),
                Static("____", id="par_target"),
                Static(" 的 "),
                Button("家长列表", id="btn_par", disabled=True),
                id="act3",
            ),
            id="actions",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.title = "云班 yunban TUI"
        self._raw(None)
        self.students, self.events = [], []
        self._load_classes()

    # ---- 数据加载 ----

    def action_invalidate_cache(self):
        """手动失效全部缓存：清空内存缓存 + 删除磁盘缓存，随后重拉班级与当前班数据"""
        CACHE.clear()
        try:
            os.remove(CACHE_FILE)
        except OSError:
            pass
        self.query_one("#info", Static).update("缓存已清空，重新拉取数据...")
        self._load_classes()
        if self.sel_class:
            self._load_students()
            self._load_events()

    @work
    async def _load_classes(self):
        raw, net = await asyncio.to_thread(fetch, URL_CLASSES)
        self.class_items = raw.get("data") or []
        clist = self.query_one("#cls_list", OptionList)
        clist.clear_options()
        for i, c in enumerate(self.class_items):
            clist.add_option(Option(c["name"], id=str(i)))
        self._raw(raw) if net else None

    # ---- 选中回调 ----

    def _on_class_select(self, cls):
        self.sel_class = cls
        self.sel_student = None
        self.sel_event = None
        self.sel_parent = None
        # 切换班级：清空旧班学生/事件/家长列表，避免残留数据误读
        self.students, self.events, self.parents = [], [], []
        self.query_one("#stu_list", OptionList).clear_options()
        self.query_one("#evt_list", OptionList).clear_options()
        self.query_one("#par_list", OptionList).clear_options()
        self.query_one("#cls_name", Static).update(cls["name"])
        self.query_one("#target_name", Static).update(cls["name"])
        self.query_one("#info", Static).update(
            f"班级：{cls['name']} · roomUid={cls['roomUid']} · uid={cls['uid']}"
        )
        self._refresh_action_buttons()
        self._update_target()
        self._update_parent()
        self._update_attend()
        # 命中缓存才自动加载；未缓存则保持按钮可用，由用户手动拉取
        if ("GET", _students_url(self.sel_class["uid"])) in CACHE:
            self._load_students()
        if ("GET", _events_url(self.sel_class["roomUid"])) in CACHE:
            self._load_events()

    def _on_student_select(self, stu):
        self.sel_student = stu
        self.sel_parent = None
        self.query_one("#info", Static).update(_describe_student(stu))
        # 家长信息不自动拉取：清空旧数据，由用户点「家长列表」按钮手动获取
        self.parents = []
        self.query_one("#par_list", OptionList).clear_options()
        self._update_target()
        self._update_parent()
        self._update_attend()

    def _on_event_select(self, evt):
        self.sel_event = evt
        self.query_one("#info", Static).update(_describe_event(evt))
        self._update_attend()

    def _on_parent_select(self, parent):
        self.sel_parent = parent
        self.query_one("#info", Static).update(_describe_parent(parent))

    # ---- 按钮状态 ----

    def _refresh_action_buttons(self):
        # 学生/考勤按钮始终可用：点击即强制重拉列表（不走缓存）
        self.query_one("#btn_stu", Button).disabled = False
        self.query_one("#btn_evt", Button).disabled = False

    def _update_target(self):
        name = (
            self.sel_student["name"]
            if self.sel_student
            else (self.sel_class["name"] if self.sel_class else "")
        )
        self.query_one("#target_name", Static).update(name or "?")

    def _update_parent(self):
        has = self.sel_student is not None
        self.query_one("#btn_par", Button).disabled = not has
        name = self.sel_student["name"] if has else ""
        self.query_one("#par_target", Static).update(name or "____")

    def _update_attend(self):
        ready = self.sel_event is not None and (
            self.sel_student is not None or self.sel_class is not None
        )
        self.query_one("#btn_attend", Button).disabled = not ready

    # ---- 按钮点击 ----

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "btn_stu":
            CACHE.pop(("GET", _students_url(self.sel_class["uid"])), None)
            _save_cache()
            self._load_students()
        elif event.button.id == "btn_evt":
            CACHE.pop(("GET", _events_url(self.sel_class["roomUid"])), None)
            _save_cache()
            self._load_events()
        elif event.button.id == "btn_par":
            if not self.sel_student:
                return
            CACHE.pop(("GET", _parents_url(self.sel_student["uid"])), None)
            _save_cache()
            self._load_parents()
        elif event.button.id == "btn_attend":
            self._do_attend()

    @work
    async def _load_students(self):
        if not self.sel_class:
            return
        url = _students_url(self.sel_class["uid"])
        raw, net = await asyncio.to_thread(fetch, url)
        data = (
            raw["data"][0].get("students", [])
            if isinstance(raw.get("data"), list) and raw["data"]
            else []
        )
        self.students = data
        ol = self.query_one("#stu_list", OptionList)
        ol.clear_options()
        for i, s in enumerate(data):
            label = f"{s.get('name', '')}  {s.get('sid', '')}".rstrip()
            ol.add_option(Option(label, id=str(i)))
        if net:
            self._raw(raw)

    @work
    async def _load_events(self):
        if not self.sel_class:
            return
        url = _events_url(self.sel_class["roomUid"])
        raw, net = await asyncio.to_thread(fetch, url)
        self.events = raw.get("data") or []
        ol = self.query_one("#evt_list", OptionList)
        ol.clear_options()
        for i, e in enumerate(self.events):
            name = e.get("name") or e.get("eventName") or f"(事件{i})"
            try:
                s, en = yunban_client.geteventtime(YunbanEvent.from_dict(e))
            except Exception:
                s, en = "", e.get("endTime", "")
            label = f"{name}  [{s} ~ {en}]" if s else name
            ol.add_option(Option(label, id=str(i)))
        if net:
            self._raw(raw)
        self._update_attend()

    @work
    async def _load_parents(self):
        if not self.sel_student:
            return
        url = _parents_url(self.sel_student["uid"])
        raw, net = await asyncio.to_thread(fetch, url)
        self.parents = raw.get("data") or []
        ol = self.query_one("#par_list", OptionList)
        ol.clear_options()
        for i, p in enumerate(self.parents):
            label = f"{p.get('parentName', '')}  ·未读{p.get('notReadNoteCount', 0)}"
            ol.add_option(Option(label, id=str(i)))
        if net:
            self._raw(raw)

    @work
    async def _do_attend(self):
        if not (self.sel_event and (self.sel_student or self.sel_class)):
            return
        event = self.sel_event
        evt = YunbanEvent.from_dict(event)
        start, end = yunban_client.geteventtime(evt)

        objects = [self.sel_student] if self.sel_student else self.students
        if not objects:
            self.query_one("#info", Static).update("[ERROR] 无考勤对象，先加载学生列表")
            return

        import time as _time

        date_now = _time.strftime("%Y-%m-%d", _time.localtime())
        pb = self.query_one("#pb", ProgressBar)
        pb.progress, pb.total = 0, len(objects)
        pb.styles.display = "block"
        self.query_one("#info", Static).update(
            f"开始考勤 {event.get('name', event.get('eventName', ''))} ..."
        )

        for i, s in enumerate(objects):
            fake_time = yunban_client.randomtime(evt)
            payload = {
                "attendanceData": [
                    {
                        "eventId": evt.eventId,
                        "eventVersion": 1,
                        "attendanceType": 1,
                        "forwardEventType": 10,
                        "eventStartTime": start,
                        "eventAttendTime": evt.endTime,
                        "eventEndTime": end,
                        "classUid": self.sel_class["uid"],
                        "attendanceDate": date_now,
                        "attendanceTime": fake_time,
                        "roomUid": self.sel_class["roomUid"],
                        "userUid": s["uid"],
                        "userName": s["name"],
                        "userSid": s.get("sid", ""),
                    }
                ]
            }
            resp = await asyncio.to_thread(_post_attend, payload)
            self.query_one("#info", Static).update(
                f"{s['name']} {date_now} {fake_time} -> {resp.get('statusCode', resp)}"
            )
            pb.progress = i + 1
        pb.styles.display = "none"
        self.query_one("#info", Static).update(f"考勤完成，共 {len(objects)} 人")

    # ---- 输入框：按名称快选班级/学生 ----

    def on_input_submitted(self, event: Input.Submitted):
        key = (event.value or "").strip()
        event.input.value = ""
        if not key:
            return
        for c in self.class_items:
            if key in c["name"]:
                self._on_class_select(c)
                return
        for s in self.students:
            if key in s["name"]:
                self._on_student_select(s)
                return

    # ---- OptionList 选中事件 ----

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        lst = event.option_list.id
        idx = event.option_index
        if lst == "cls_list" and 0 <= idx < len(self.class_items):
            self._on_class_select(self.class_items[idx])
        elif lst == "stu_list" and 0 <= idx < len(self.students):
            self._on_student_select(self.students[idx])
        elif lst == "evt_list" and 0 <= idx < len(self.events):
            self._on_event_select(self.events[idx])
        elif lst == "par_list" and 0 <= idx < len(self.parents):
            self._on_parent_select(self.parents[idx])

    def _raw(self, raw):
        text = (
            json.dumps(raw, ensure_ascii=False, indent=2)
            if raw is not None
            else "(无原始返回)"
        )
        self.query_one("#raw_text", Static).update(text)


if __name__ == "__main__":
    YunbanTUI().run()
