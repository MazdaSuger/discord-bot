import os, json, asyncio, datetime as dt
import pytz
from discord import Intents, app_commands, Interaction, Object, Thread
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

JST = pytz.timezone("Asia/Tokyo")
DATA_FILE = "report_data.json"

def now_jst():
    return dt.datetime.now(JST)

def load_db():
    if not os.path.exists(DATA_FILE):
        return {"reports": {}}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_db(db):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

db = load_db()

intents = Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree
scheduler = AsyncIOScheduler(timezone=JST)

# ------------- ユーティリティ -------------
def report_key(guild_id: int, user_id: int):
    return f"{guild_id}:{user_id}"

async def ensure_thread(inter: Interaction, title: str):
    # 課題ごとにスレッドを作る（なければ作成）
    key = report_key(inter.guild_id, inter.user.id)
    r = db["reports"].get(key)
    if r and r.get("thread_id"):
        th = inter.channel.get_thread(r["thread_id"])
        if th: return th
    # 新規作成
    start_msg = await inter.channel.send(f"**研究スレッド**: {title}（{inter.user.mention}）")
    thread = await start_msg.create_thread(name=f"　{title} / {inter.user.display_name}")
    # 保存
    db["reports"].setdefault(key, {})
    db["reports"][key]["thread_id"] = thread.id
    save_db(db)
    return thread

def parse_date_jst(date_str: str):
    # "2025-11-30 23:59" or "2025-11-30"
    try:
        if " " in date_str:
            d = dt.datetime.strptime(date_str, "%Y-%m-%d %H:%M")
        else:
            d = dt.datetime.strptime(date_str, "%Y-%m-%d")
            d = d.replace(hour=23, minute=59)
        return JST.localize(d)
    except ValueError:
        return None

async def schedule_reminders(inter: Interaction, key: str):
    r = db["reports"].get(key)
    if not r: return
    deadline = parse_date_jst(r["deadline"])
    thread_id = r.get("thread_id")
    if not (deadline and thread_id): return

    # 既存ジョブ削除
    for jid in list(r.get("jobs", [])):
        try: scheduler.remove_job(jid)
        except: pass
    r["jobs"] = []

    # マイルストーン（2週間前 / 1週間前 / 3日前 / 前日 / 当日朝）
    checkpoints = [
        ("2w", deadline - dt.timedelta(days=14)),
        ("1w", deadline - dt.timedelta(days=7)),
        ("3d", deadline - dt.timedelta(days=3)),
        ("1d", deadline - dt.timedelta(days=1)),
        ("0d", deadline.replace(hour=7, minute=45)),
    ]
    for tag, when in checkpoints:
        if when > now_jst():
            jid = f"{key}:{tag}"
            scheduler.add_job(send_checkpoint, DateTrigger(run_date=when), id=jid,
                              args=[inter.guild_id, thread_id, tag, r])
            r["jobs"].append(jid)

    # 毎週の進捗プロンプト（火曜19:00）
    weekly = IntervalTrigger(weeks=1, start_date=now_jst() + dt.timedelta(seconds=5))
    jidw = f"{key}:weekly"
    scheduler.add_job(send_weekly_ping, weekly, id=jidw,
                      args=[inter.guild_id, thread_id, r])
    r["jobs"].append(jidw)

    save_db(db)

async def send_checkpoint(guild_id: int, thread_id: int, tag: str, r: dict):
    guild = bot.get_guild(guild_id)
    if not guild: return
    thread: Thread = guild.get_thread(thread_id)
    if not thread: return
    messages = {
        "2w": "⏳ **締切2週間前**：テーマ・構成・資料、どこから始めますか？",
        "1w": "🧱 **締切1週間前**：本文の“見出し・要旨”だけ先に立てましょう。中身は後でOKです。",
        "3d": "📝 **締切3日前**：下書きを“段落ごとに1文”でOK。まず並べましょう。",
        "1d": "🧹 **前日**：引用・参考文献の最終チェック！",
        "0d": "🚀 **当日朝**：提出フォームを開いて“ファイルを置く”ところまで突っ走る！",
    }
    await thread.send(messages.get(tag, "⏰ マイルストーンです。何か1分で進めましょう。"))

async def send_weekly_ping(guild_id: int, thread_id: int, r: dict):
    guild = bot.get_guild(guild_id)
    if not guild: return
    thread: Thread = guild.get_thread(thread_id)
    if not thread: return
    await thread.send("🔁 週次チェック：\n"
                      "1) **いまの一歩**：何を“最初の1分”でやる？\n"
                      "2) **ブロッカー**：作業が止まっているなら、理由は？（理解/資料/気分/時間）\n"
                      "3) **松田に頼む**：資料探す？表現直す？")

# ------------- Slash Commands -------------

@tree.command(name="start_report", description="レポートを開始（テーマ仮＋締切設定）")
@app_commands.describe(
    theme="テーマ（未定なら '未定'）",
    deadline="締切（例：2025-11-30 または 2025-11-30 23:59）"
)
async def start_report(inter: Interaction, theme: str, deadline: str):
    await inter.response.defer(ephemeral=True)
    key = report_key(inter.guild_id, inter.user.id)
    db["reports"][key] = {
        "user_id": inter.user.id,
        "guild_id": inter.guild_id,
        "theme": theme,
        "deadline": deadline,
        "progress": [],
        "milestones": {
            "テーマ": False, "構成": False, "下書き": False, "清書": False, "提出": False
        }
    }
    save_db(db)
    thread = await ensure_thread(inter, theme if theme != "未定" else "進捗管理")
    await schedule_reminders(inter, key)
    await inter.followup.send("✅ 進捗管理を開始。スレッドを作りました。", ephemeral=True)
    await thread.send(
        f"スタート！\n"
        f"- テーマ: {theme}\n- 締切: {deadline}\n\n"
        f"まずは `/brainstorm` で“モヤ”を言語化しよう。"
    )

@tree.command(name="brainstorm", description="テーマ探索の質問プロンプトを出す")
async def brainstorm(inter: Interaction):
    await inter.response.defer(ephemeral=True)
    thread = await ensure_thread(inter, "テーマ探索")
    prompt = (
        "💡 テーマ探索ワーク（3分）\n"
        "1) 身近で“なんか変、苦手”だと思うことを3つ（例：校則/労働/SNS/バイト）\n"
        "2) 1つ選んで なぜ？ を3回掘る（なぜそれが気になる？→なぜ不公平だと感じる？→誰に影響？）\n"
        "3) 仮タイトルを書いてみる：『○○は誰のため？—△△の観点から』\n"
        "終わったら `/set_theme` で仮テーマを登録しよう。"
    )
    await thread.send(prompt)
    await inter.followup.send("スレッドにブレスト用プロンプトを投下しました。", ephemeral=True)

@tree.command(name="set_theme", description="仮テーマを保存/更新する")
@app_commands.describe(theme="仮テーマ")
async def set_theme(inter: Interaction, theme: str):
    await inter.response.defer(ephemeral=True)
    key = report_key(inter.guild_id, inter.user.id)
    r = db["reports"].setdefault(key, {})
    r["theme"] = theme
    save_db(db)
    thread = await ensure_thread(inter, theme)
    await thread.send(f"🎯 **仮テーマ更新**：{theme}\n次は `/outline` で構成を立てよう。")
    await inter.followup.send("テーマを更新しました。", ephemeral=True)

@tree.command(name="outline", description="構成テンプレートを挿入する")
async def outline(inter: Interaction):
    await inter.response.defer(ephemeral=True)
    thread = await ensure_thread(inter, "構成")
    template = (
        "**構成テンプレート**\n"
        "1. 背景/きっかけ（100-150字）\n"
        "2. 問い（1-2文）\n"
        "3. 方法（文献/観察など）\n"
        "4. 結果（箇条書きでOK）\n"
        "5. 考察（“なぜ/だから”）\n"
        "6. 参考文献（最低3件）\n"
        "まずは各項目“1行”でOK。 `/log` で進捗を刻みましょう。"
    )
    await thread.send(template)
    await inter.followup.send("構成テンプレートを投下しました。", ephemeral=True)

@tree.command(name="log", description="進捗を記録（1行でもOK）")
@app_commands.describe(note="進捗メモ")
async def log_progress(inter: Interaction, note: str):
    await inter.response.defer(ephemeral=True)
    key = report_key(inter.guild_id, inter.user.id)
    r = db["reports"].setdefault(key, {})
    r.setdefault("progress", []).append({"ts": now_jst().isoformat(), "note": note})
    save_db(db)
    thread = await ensure_thread(inter, r.get("theme", "レポート"))
    await thread.send(f"🧭 **進捗**：{note}")
    await inter.followup.send("進捗を記録しました。", ephemeral=True)

@tree.command(name="status", description="状況表示（テーマ/締切/直近の進捗）")
async def status(inter: Interaction):
    await inter.response.defer(ephemeral=True)
    key = report_key(inter.guild_id, inter.user.id)
    r = db["reports"].get(key)
    if not r:
        await inter.followup.send("まだ `/start_report` が未実行です。", ephemeral=True); return
    recent = r.get("progress", [])[-3:]
    txt = (f"📊 **現在地**\n"
           f"- テーマ: {r.get('theme','未定')}\n"
           f"- 締切: {r.get('deadline','未設定')}\n"
           f"- 直近の進捗: " + (", ".join(p['note'] for p in recent) if recent else "（なし）"))
    await inter.followup.send(txt, ephemeral=True)

@tree.command(name="set_deadline", description="締切を設定/変更")
@app_commands.describe(deadline="例：2025-11-30 23:59")
async def set_deadline(inter: Interaction, deadline: str):
    await inter.response.defer(ephemeral=True)
    d = parse_date_jst(deadline)
    if not d:
        await inter.followup.send("日付の形式が不正です。`YYYY-MM-DD [HH:MM]`", ephemeral=True); return
    key = report_key(inter.guild_id, inter.user.id)
    r = db["reports"].setdefault(key, {})
    r["deadline"] = d.strftime("%Y-%m-%d %H:%M")
    save_db(db)
    await schedule_reminders(inter, key)
    await inter.followup.send(f"締切を **{r['deadline']} JST** に設定しました。", ephemeral=True)

@tree.command(name="mark", description="マイルストーン達成（テーマ/構成/下書き/清書/提出）")
@app_commands.describe(step="テーマ/構成/下書き/清書/提出")
async def mark(inter: Interaction, step: str):
    await inter.response.defer(ephemeral=True)
    if step not in ["テーマ","構成","下書き","清書","提出"]:
        await inter.followup.send("step は「テーマ/構成/下書き/清書/提出」から選択。", ephemeral=True); return
    key = report_key(inter.guild_id, inter.user.id)
    r = db["reports"].setdefault(key, {})
    r.setdefault("milestones", {})[step] = True
    save_db(db)
    thread = await ensure_thread(inter, r.get("theme","レポート"))
    await thread.send(f"**{step} 完了**！ 次の一歩は `/outline` や `/log` で刻みましょう。")
    await inter.followup.send(f"{step} を達成にしました。", ephemeral=True)

@tree.command(name="nudge", description="やさしい着火プロンプトを投下")
async def nudge(inter: Interaction):
    await inter.response.defer(ephemeral=True)
    key = report_key(inter.guild_id, inter.user.id)
    r = db["reports"].get(key, {})
    thread = await ensure_thread(inter, r.get("theme","レポート"))
    msg = ("**最初の1分**：今やるのは “ファイルを開く/1行打つ/参考文献を1件貼る” のどれですか？\n"
           "完璧禁止、開始だけでOK。")
    await thread.send(msg)
    await inter.followup.send("着火メッセージを送りました。", ephemeral=True)
# ==== 1) データ構造に done_logs を追加（起動時ロード後あたり） ====
db = load_db()
db.setdefault("done_logs", {})  # key = f"{guild}:{user}" -> {"YYYY-MM-DD": [str, ...]}

def du_key(guild_id: int, user_id: int):
    return f"{guild_id}:{user_id}"

def jst_date_str(dtobj=None):
    if dtobj is None: dtobj = now_jst()
    return dtobj.strftime("%Y-%m-%d")

# ==== 2) できたことを記録する内部関数 ====
def add_done(guild_id: int, user_id: int, text: str, date_str=None):
    if date_str is None:
        date_str = jst_date_str()
    key = du_key(guild_id, user_id)
    bucket = db["done_logs"].setdefault(key, {})
    bucket.setdefault(date_str, []).append(text)
    save_db(db)

def get_done(guild_id: int, user_id: int, date_str=None):
    if date_str is None:
        date_str = jst_date_str()
    key = du_key(guild_id, user_id)
    return db["done_logs"].get(key, {}).get(date_str, [])

def count_done(guild_id: int, user_id: int, date_str):
    return len(get_done(guild_id, user_id, date_str))

def seven_days_counts(guild_id: int, user_id: int):
    today = now_jst().date()
    days = []
    for i in range(6, -1, -1):
        d = today - dt.timedelta(days=i)
        s = d.strftime("%Y-%m-%d")
        days.append((s, count_done(guild_id, user_id, s)))
    return days

def calc_streak(guild_id: int, user_id: int):
    # “0件の日が来るまで”連続カウント（今日から過去へ）
    today = now_jst().date()
    key = du_key(guild_id, user_id)
    bucket = db["done_logs"].get(key, {})
    streak = 0
    d = today
    while True:
        s = d.strftime("%Y-%m-%d")
        if bucket and bucket.get(s) and len(bucket.get(s)) > 0:
            streak += 1
            d = d - dt.timedelta(days=1)
        else:
            break
    return streak

def ascii_bar(n, max_len=10):
    n = min(n, max_len)
    return "█" * n + "·" * (max_len - n)

# ==== 3) Slash Commands ====
@tree.command(name="done", description="今日の『できたこと』を1件記録")
@app_commands.describe(text="短くてOK。例: 背景100字を書いた / 参考2本メモ / 提出フォームを開いた")
async def done_cmd(inter: Interaction, text: str):
    await inter.response.defer(ephemeral=True)
    add_done(inter.guild_id, inter.user.id, text)
    thread = await ensure_thread(inter, "できたこと")
    await thread.send(f"✅ **できた**：{text}（{jst_date_str()}）")
    total_today = len(get_done(inter.guild_id, inter.user.id))
    await inter.followup.send(f"今日の件数：{total_today}件。お疲れ様！", ephemeral=True)

@tree.command(name="today", description="今日の『できたこと』一覧を表示")
async def today_cmd(inter: Interaction):
    await inter.response.defer(ephemeral=True)
    items = get_done(inter.guild_id, inter.user.id)
    date = jst_date_str()
    if not items:
        await inter.followup.send(f"{date} はまだ未登録。`/done` で1件だけ書きましょう。", ephemeral=True)
        return
    lines = "\n".join([f"- {t}" for t in items])
    await inter.followup.send(f"**{date} のできたこと（{len(items)}件）**\n{lines}", ephemeral=True)

@tree.command(name="done_week", description="直近7日の日別件数を表示")
async def done_week_cmd(inter: Interaction):
    await inter.response.defer(ephemeral=True)
    rows = seven_days_counts(inter.guild_id, inter.user.id)
    text = "\n".join([f"{d} : {c}件" for d, c in rows])
    await inter.followup.send("**直近7日（件数）**\n" + text, ephemeral=True)

@tree.command(name="streak", description="連続記録と簡易グラフを表示")
async def streak_cmd(inter: Interaction):
    await inter.response.defer(ephemeral=True)
    streak = calc_streak(inter.guild_id, inter.user.id)
    rows = seven_days_counts(inter.guild_id, inter.user.id)
    graph = "\n".join([f"{d[5:]} | {ascii_bar(c)} ({c})" for d, c in rows])  # MM-DD 表示
    msg = (f"🔥 **連続記録**：{streak} 日\n"
           f"📈 **7日ミニグラフ**\n{graph}\n"
           "（棒は最大10件まで表示）")
    await inter.followup.send(msg, ephemeral=True)

# ==== 4) 毎日21:30JSTの自動プロンプト ====
async def nightly_ping():
    # DB上で「reports」に登録があるユーザにだけ投下
    for key, r in db.get("reports", {}).items():
        guild_id = r.get("guild_id"); thread_id = r.get("thread_id")
        if not (guild_id and thread_id): continue
        guild = bot.get_guild(guild_id)
        if not guild: continue
        thread = guild.get_thread(thread_id)
        if not thread: continue
        await thread.send("🌙 **できたことチェック（1分）**\n"
                          "今日の“できた”を **3つ** `/done` で送ってみましょう。\n"
                          "例) `/done 背景100字を書いた` `/done 資料リンクを1本貼った` `/done 見出しを1行追加`")

def schedule_nightly_job():
    # 毎日21:30に実行
    today = now_jst().date()
    first = dt.datetime.combine(today, dt.time(hour=21, minute=30))
    first = JST.localize(first)
    if first <= now_jst():
        first = first + dt.timedelta(days=1)
    scheduler.add_job(nightly_wrap, DateTrigger(run_date=first), id="nightly_first")

async def nightly_wrap():
    await nightly_ping()
    # 以降は24時間ごと
    scheduler.add_job(nightly_ping, IntervalTrigger(days=1), id="nightly_loop")

# ==== 5) on_ready で夜間ジョブの初期化を追加 ====
@bot.event
async def on_ready():
    try:
        await tree.sync()
        print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    except Exception as e:
        print("Sync error:", e)
    if not scheduler.running:
        scheduler.start()
    # 追加：
    try:
        scheduler.remove_job("nightly_first")
    except: pass
    try:
        scheduler.remove_job("nightly_loop")
    except: pass
    schedule_nightly_job()
@bot.event
async def on_ready():
    try:
        await tree.sync()
        print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    except Exception as e:
        print("Sync error:", e)
    if not scheduler.running:
        scheduler.start()

if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_BOT_TOKEN")
    if not TOKEN:
        raise SystemExit("環境変数 DISCORD_BOT_TOKEN を設定してください。")
    bot.run(TOKEN)