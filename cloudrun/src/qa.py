from llm_client import ANSWER_MODEL, call_llm
from icons import WARN
from organize import get_doc_content, to_line_plaintext

SYSTEM_PROMPT = """你是旅行規劃助手的問答引擎。你會收到一份旅程的markdown整理文件，以及使用者的問題。

規則：
1. 只根據提供的文件內容回答，不要用自己的知識或猜測來補充事實。
2. 文件裡通常帶有來源引用（人名+日期），回答時可以簡短帶到是誰提的、大概什麼時候，不用逐字複製引用格式。
3. 如果文件裡找不到答案，或這件事還列在未定事項、尚未拍板，就明確說「還沒決定」或「目前查無相關資料」，不要瞎猜或編造。
4. 回答簡短口語，像在群組裡回話，不要長篇大論。
5. 這則回覆會直接顯示在LINE聊天室裡，LINE不會渲染markdown語法：不要用**粗體**、不要用_斜體_、不要用#標題、不要用markdown表格，就用純文字。"""


async def answer_question(env, trip_id: str, question: str) -> str:
    doc = await get_doc_content(env, trip_id)
    if not doc:
        return WARN + "這趟旅程目前還沒有整理出任何內容"

    trip_row = await env.db.query("SELECT answer_model FROM trips WHERE id = ?", [trip_id])
    model = (trip_row.results[0]["answer_model"] if trip_row.results else None) or ANSWER_MODEL

    user_content = f"旅程文件：\n{doc}\n\n---\n\n問題：{question}"
    answer = await call_llm(env, "answer", trip_id, model, SYSTEM_PROMPT, user_content, max_tokens=1024)
    answer = to_line_plaintext(answer.strip())
    return answer or "🤔 不確定，文件裡沒有找到相關資訊"
