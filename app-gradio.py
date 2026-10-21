"""A customer service agent for a gym (Gradio app).

Every question goes to the agent first. The agent decides which tool to use:
shop information and prices come from a SQLite database, the code calculates the fee,
and anything the tools cannot answer is handed to a human.
The panel on the right shows which tools were called and what they returned.

Run it with:  python app-gradio.py
"""

import csv
import json
import os
import re
from pathlib import Path
import sqlite3

import gradio as gr
from openai import OpenAI
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# 1. API setup
# ---------------------------------------------------------------------------
# Settings come from the .env file in this folder (copy .env.example to .env first).
load_dotenv(Path(__file__).parent / ".env")
CHAT_MODEL = os.getenv("MODEL_NAME")
client = OpenAI(api_key=os.getenv("API_KEY"), base_url=os.getenv("LLM_API_URL"), timeout=60.0)


# ---------------------------------------------------------------------------
# 2. The gym database: two tables, gym_info and classes (SQLite)
# ---------------------------------------------------------------------------
# check_same_thread=False because Gradio answers requests on worker threads.
connection = sqlite3.connect("gym_app.db", check_same_thread=False)
connection.row_factory = sqlite3.Row
cursor = connection.cursor()

cursor.execute("DROP TABLE IF EXISTS gym_info")
cursor.execute("CREATE TABLE gym_info (topic TEXT PRIMARY KEY, content TEXT NOT NULL)")
cursor.execute("DROP TABLE IF EXISTS classes")
cursor.execute("""CREATE TABLE classes (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, price INTEGER, available TEXT)""")

# 讀取剛剛建立的 gym_info.csv 與 classes.csv
with open("gym_info.csv", newline="", encoding="utf-8-sig") as f:
    cursor.executemany("INSERT INTO gym_info VALUES (?, ?)",
                       [(r["topic"], r["content"]) for r in csv.DictReader(f)])
with open("classes.csv", newline="", encoding="utf-8-sig") as f:
    cursor.executemany("INSERT INTO classes (name, price, available) VALUES (?, ?, ?)",
                       [(r["name"], r["price"], r["available"]) for r in csv.DictReader(f)])
connection.commit()
SHOP_TOPICS = [r["topic"] for r in cursor.execute("SELECT topic FROM gym_info")]
print(f"Ready: {len(SHOP_TOPICS)} gym topics.")


# ---------------------------------------------------------------------------
# 3. The tools, as plain functions, and what the model sees
# ---------------------------------------------------------------------------
def get_gym_info(topic):
    """Return one piece of gym information, such as the opening hours."""
    row = cursor.execute("SELECT content FROM gym_info WHERE topic = ?", (topic,)).fetchone()
    if row is None:
        return {"found": False, "topics_available": SHOP_TOPICS}
    return {"found": True, "topic": topic, "content": row["content"]}


def check_class(class_name):
    """Find classes or memberships whose name contains the words, and return their prices and availability."""
    like = f"%{class_name.strip()}%"
    rows = cursor.execute("SELECT name, price, available FROM classes WHERE name LIKE ?", (like,)).fetchall()
    if not rows:
        return {"found": False, "note": f"'{class_name}' is not in our system."}
    return {"found": True, "items": [{"name": r["name"], "price": r["price"],
                                      "available": str(r["available"]).lower() == "true"} for r in rows]}


def calculate_fee(price, months):
    """Calculate the total fee for a membership over several months.
    If months >= 3, apply a 10% discount."""
    try:
        price = int(price)
        months = int(months)
        total = price * months
        if months >= 3:
            discount_total = int(total * 0.9)
            return {"lines": [f"{months} months x NT${price} = NT${total}"], 
                    "discount": "10% off for 3+ months", 
                    "total": discount_total}
        return {"lines": [f"{months} months x NT${price} = NT${total}"], 
                "discount": "None", 
                "total": total}
    except Exception as e:
        return {"total": None, "note": "Calculation error. Make sure price and months are numbers."}


def transfer_to_human():
    """Hand the conversation to a person."""
    return {"message": "Please call our staff at 07-123-4567, or contact our LINE official account @energygym.",
            "note": ("The hand-over is done. First tell the customer in one sentence why, "
                     "then give this message. Do not say that you will connect them: you cannot. "
                     "Do not call this tool again.")}


TOOL_FUNCTIONS = {
    "get_gym_info": get_gym_info,
    "check_class": check_class,
    "calculate_fee": calculate_fee,
    "transfer_to_human": transfer_to_human,
}

TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "get_gym_info",
        "description": ("Get one piece of gym information: opening hours, parking, payment, trial, refund rules, lockers, or facilities."),
        "parameters": {"type": "object",
                       "properties": {"topic": {"type": "string", "enum": SHOP_TOPICS}},
                       "required": ["topic"]}}},
    {"type": "function", "function": {
        "name": "check_class",
        "description": ("Get the price of a class or membership, and whether it is available. "
                        "Use a short name, such as '瑜珈' or '單月會籍'."),
        "parameters": {"type": "object",
                       "properties": {"class_name": {"type": "string"}},
                       "required": ["class_name"]}}},
    {"type": "function", "function": {
        "name": "calculate_fee",
        "description": "Calculate the total fee for buying a membership for multiple months. Provide the monthly price and the number of months.",
        "parameters": {"type": "object",
                       "properties": {"price": {"type": "integer"}, "months": {"type": "integer"}},
                       "required": ["price", "months"]}}},
    {"type": "function", "function": {
        "name": "transfer_to_human",
        "description": "Use this for complaints, requests for a human agent, or gym questions that no other tool can answer.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
]

SYSTEM_PROMPT = """
You are the friendly customer service agent of Energy Gym (活力健身房).
All prices are in New Taiwan dollars, written like NT$1,000.

RULES
1. Answer only from tool results. Never answer from memory, and never invent any classes or prices.
2. If you cannot find the answer, say so plainly. Do not guess.
3. Only talk about this gym. Politely refuse other topics, such as restaurants or politics.
4. Answer politely, in three sentences or less. Use Traditional Chinese (繁體中文).
5. Gym rules and info (opening hours, parking, payment, trial, facilities) -> get_gym_info.
6. Class prices and availability -> check_class.
7. Calculating membership fees for multiple months -> calculate_fee.
8. Complaints or questions no tool can answer -> transfer_to_human.
"""

MAX_STEPS = 6


def run_agent(chat_history):
    """Answer the last question in chat_history. Returns (answer, trace)."""
    question = chat_history[-1]["content"]
    messages = ([{"role": "system", "content": SYSTEM_PROMPT}] + chat_history[:-1]
                + chat_history[-1:])
    trace = []

    for step in range(MAX_STEPS):
        message = client.chat.completions.create(
            model=CHAT_MODEL, messages=messages, tools=TOOL_SPECS, temperature=0.2,
        ).choices[0].message

        if not message.tool_calls:
            return (message.content or "").strip(), trace

        messages.append({"role": "assistant", "content": message.content or "",
                         "tool_calls": [{"id": c.id, "type": "function",
                                         "function": {"name": c.function.name,
                                                      "arguments": c.function.arguments}}
                                        for c in message.tool_calls]})
        for call in message.tool_calls:
            arguments = json.loads(call.function.arguments or "{}")
            result = TOOL_FUNCTIONS[call.function.name](**arguments)
            trace.append({"step": step + 1, "tool": call.function.name, "arguments": arguments, "result": result})
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": json.dumps(result, ensure_ascii=False)})

    return "不好意思，系統出現問題。請撥打 07-123-4567 聯絡客服。", trace


# ---------------------------------------------------------------------------
# 4. The Gradio page
# ---------------------------------------------------------------------------
GREETING = "您好！我是活力健身房 (Energy Gym) 的線上客服，請問有什麼我可以幫您的？您可以詢問營業時間、課程費用或是計算會籍喔！"

EMPTY_TRACE = "### Tools used\n\n(nothing yet)"

SAMPLE_GROUPS_ZH = {
    "健身房資訊 (get_gym_info)": [
        "請問你們幾點開門？",
        "這附近有停車位嗎？",
        "有提供置物櫃嗎？",
    ],
    "課程與價格 (check_class)": [
        "請問單次入場多少錢？",
        "週一晚上的瑜珈班還有名額嗎？",
        "飛輪班還有位置嗎？",
    ],
    "計算會籍費用 (calculate_fee)": [
        "我要買單月會籍，連續買 4 個月總共要多少錢？",
        "單月會籍買 2 個月有打折嗎？",
    ],
    "客訴、無關問題 (transfer_to_human)": [
        "你們的教練態度很差，我要找主管！",
        "你們館長叫什麼名字？",
        "這附近有什麼好吃的牛肉麵嗎？",
    ],
}


def format_trace(trace):
    if not trace:
        return "### Tools used\n\n(no tool was needed for this answer)"
    lines = ["### Tools used", ""]
    for entry in trace:
        lines += [f"**{entry['step']}. {entry['tool']}**", "",
                  f"arguments: `{json.dumps(entry['arguments'], ensure_ascii=False)[:160]}`", "",
                  f"result: `{json.dumps(entry['result'], ensure_ascii=False)[:260]}`", ""]
    return "\n".join(lines)


def start_chat():
    return [{"role": "assistant", "content": GREETING}]


def add_user_message(user_text, chat_history):
    user_text = user_text.strip()
    if not user_text:
        return "", chat_history
    return "", chat_history + [{"role": "user", "content": user_text}]


def answer_question(chat_history):
    if not chat_history or chat_history[-1]["role"] != "user":
        return chat_history, EMPTY_TRACE
    try:
        answer, trace = run_agent(chat_history)
        panel = format_trace(trace)
    except Exception as error:
        answer = f"(系統訊息) API 發生錯誤: {error}"
        panel = "### Tools used\n\n(the call failed)"
    return chat_history + [{"role": "assistant", "content": answer}], panel


with gr.Blocks(title="Energy Gym Agent") as demo:
    gr.Markdown(
        "## 活力健身房 (Energy Gym) 線上客服\n"
        "本系統會自動判斷要使用哪個工具查詢資料庫（營業時間、課程表）或進行費用計算。無法回答的問題將引導至真人客服。"
    )

    with gr.Row():
        with gr.Column(scale=3):
            chatbot = gr.Chatbot(value=start_chat(), height=430, label="Agent")
            with gr.Row():
                user_box = gr.Textbox(placeholder="請問營業時間、課程或費用...", show_label=False, scale=8)
                send_button = gr.Button("傳送", variant="primary", scale=1)
                clear_button = gr.Button("清除", scale=1)

            gr.Markdown("**範例問題** (點選後請按傳送)")
            with gr.Tabs():
                with gr.Tab("中文對話測試"):
                    for group, questions in SAMPLE_GROUPS_ZH.items():
                        gr.Examples(examples=questions, inputs=user_box, label=group)

        with gr.Column(scale=2):
            trace_panel = gr.Markdown(EMPTY_TRACE)

    send_button.click(add_user_message, [user_box, chatbot], [user_box, chatbot]).then(
        answer_question, chatbot, [chatbot, trace_panel])
    user_box.submit(add_user_message, [user_box, chatbot], [user_box, chatbot]).then(
        answer_question, chatbot, [chatbot, trace_panel])

    clear_button.click(start_chat, outputs=chatbot).then(
        lambda: ("", EMPTY_TRACE), outputs=[user_box, trace_panel])


if __name__ == "__main__":
    demo.launch()