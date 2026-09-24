"""自动为班级列表中第 36 个班级全班签到（粗糙脚本，非生产级）。"""

import sys
import time

from yunban_token import yunban_client


def main():
    classlist = yunban_client.getclasslist()
    students = yunban_client.getstulist(classlist[35].uid)
    events = yunban_client.getevents(classlist[35].roomUid)
    uidlist = []
    for stu in students:
        # stu=yunban_client.searchstubyuid(uid,students)
        try:
            if sys.argv[1]:
                event = events[int(sys.argv[1])]
            else:
                event = events[1]
        except (IndexError, ValueError):
            event = events[1]
        # print(sys.argv[1])
        faketime = yunban_client.randomtime(event)
        datenow = time.strftime("%Y-%m-%d", time.localtime())
        timenow = time.strftime("%H:%M:%S", time.localtime())
        print(
            yunban_client.attend(
                stu.name,
                stu.uid,
                stu.sid,
                event,
                datenow,
                faketime,
                classlist[35].uid,
                classlist[35].roomUid,
            )
        )
        print(f"{stu.name}已签到，时间：{datenow} {faketime}")


if __name__ == "__main__":
    main()
