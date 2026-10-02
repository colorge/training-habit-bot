"""筋トレ報告ボット(GitHub Actions版)

起動後、締切まで10秒おきにSlackを確認する。
- REMIND_TIME になったら自分へDMを送り、DEADLINE_TIME のさぼり投稿を予約する
- DMのスレッドに自分が返信したら予約を取り消し、連続記録を返信して終了
状態はDMメッセージのmetadataに保存する(リポジトリには何も残さない)。
"""
import os
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from slack_sdk import WebClient

TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Tokyo"))
USER_ID = os.environ["SLACK_USER_ID"]
TIMES_CHANNEL_ID = os.environ["SLACK_TIMES_CHANNEL_ID"]
REMIND_TIME = os.environ.get("REMIND_TIME", "20:00")
DEADLINE_TIME = os.environ.get("DEADLINE_TIME", "23:59")
LAZY_TEXT = os.environ.get("LAZY_TEXT", "今日は筋トレをさぼっています :zzz:")
POLL_SECONDS = 10

client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])


def now():
    return datetime.now(TZ)


def at_today(hhmm):
    h, m = map(int, hhmm.split(":"))
    return now().replace(hour=h, minute=m, second=0, microsecond=0)


def meta(msg):
    m = msg.get("metadata") or {}
    return m.get("event_type"), m.get("event_payload") or {}


def load(dm):
    """DM履歴から (今日のリマインド, 報告済み日付の集合) を取り出す。"""
    oldest = (now() - timedelta(days=400)).timestamp()
    done, reminder = set(), None
    cursor = None
    while True:
        res = client.conversations_history(
            channel=dm, oldest=str(oldest), limit=200, cursor=cursor,
            include_all_metadata=True,
        )
        for msg in res["messages"]:
            kind, payload = meta(msg)
            if kind == "kintore_done":
                done.add(payload["date"])
            elif kind == "kintore_reminder" and payload["date"] == now().date().isoformat():
                reminder = {"ts": msg["ts"], **payload}
        cursor = (res.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return reminder, done


def streak(done, today):
    d = today if today.isoformat() in done else today - timedelta(days=1)
    n = 0
    while d.isoformat() in done:
        n += 1
        d -= timedelta(days=1)
    return n


def send_reminder(dm, done):
    deadline = at_today(DEADLINE_TIME)
    scheduled = client.chat_scheduleMessage(
        channel=TIMES_CHANNEL_ID, post_at=int(deadline.timestamp()), text=LAZY_TEXT
    )
    today = now().date().isoformat()
    client.chat_postMessage(
        channel=dm,
        text=(
            ":muscle: 今日の筋トレ報告をこのスレッドに返信してください。\n"
            f"{DEADLINE_TIME} までに返信がないと、timesに「{LAZY_TEXT}」と投稿されます。\n"
            f"現在の連続記録: {streak(done, now().date())}日"
        ),
        metadata={
            "event_type": "kintore_reminder",
            "event_payload": {"date": today, "scheduled_id": scheduled["scheduled_message_id"]},
        },
    )


def has_my_reply(dm, ts):
    res = client.conversations_replies(channel=dm, ts=ts, limit=50)
    return any(m.get("user") == USER_ID and not m.get("bot_id") for m in res["messages"][1:])


def confirm(dm, reminder, done):
    try:
        client.chat_deleteScheduledMessage(
            channel=TIMES_CHANNEL_ID, scheduled_message_id=reminder["scheduled_id"]
        )
    except Exception as e:
        print("cancel failed:", e)
    today = now().date()
    done = done | {today.isoformat()}
    client.chat_postMessage(
        channel=dm,
        thread_ts=reminder["ts"],
        text=(
            ":white_check_mark: 報告を確認しました。さぼり投稿は取り消しました。\n"
            f":fire: 連続 *{streak(done, today)}日*(累計 {len(done)}日)"
        ),
        metadata={"event_type": "kintore_done", "event_payload": {"date": today.isoformat()}},
    )


def main():
    dm = client.conversations_open(users=USER_ID)["channel"]["id"]
    deadline = at_today(DEADLINE_TIME)
    while now() < deadline:
        reminder, done = load(dm)
        today = now().date().isoformat()
        if today in done:
            print("already reported today")
            return
        if reminder is None:
            if now() >= at_today(REMIND_TIME) and now() < deadline - timedelta(seconds=90):
                send_reminder(dm, done)
                print("reminder sent")
        elif has_my_reply(dm, reminder["ts"]):
            confirm(dm, reminder, done)
            print("confirmed")
            return
        time.sleep(POLL_SECONDS)
    print("deadline reached")


if __name__ == "__main__":
    main()
