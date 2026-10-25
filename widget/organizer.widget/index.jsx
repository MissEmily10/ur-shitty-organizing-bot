// 🖥 Виджет «Мой день» для Übersicht (https://tracesof.net/uebersicht/): расписание на день и планы из бота.
// Адрес бота вписывается сам, когда бот присылает этот файл командой /widget. Ничего здесь менять не нужно.
// «＋» спрашивает через обычные окна macOS: что и когда, что это, разово ли. «☐» закрывает задачу.

import { css, run } from "uebersicht";

const BOT = "__BOT_URL__";
export const refreshFrequency = 5 * 60 * 1000; // каждые 5 минут
export const initialState = { data: null, error: "", busy: "", allPlans: false };

let shownDate = ""; // какой день открыт стрелками; пусто — бот сам решает (сегодня, а вечером — завтра)

const load = (dispatch) =>
  fetch(`${BOT}/day${shownDate ? `?d=${shownDate}` : ""}`)
    .then((r) => r.json())
    .then((data) => dispatch(data.error ? { type: "ERROR", error: data.error } : { type: "DATA", data }))
    .catch(() => dispatch({ type: "ERROR", error: "Нет связи с ботом — проверю через 5 минут" }));

export const command = load;

export const updateState = (event, state) => {
  switch (event.type) {
    case "DATA":
      return { ...state, data: event.data, error: "", busy: "" };
    case "ERROR":
      return { ...state, error: event.error, busy: "" };
    case "BUSY":
      return { ...state, busy: event.text };
    case "TOGGLE_PLANS":
      return { ...state, allPlans: !state.allPlans };
    default:
      return state;
  }
};

// ---------- окна macOS для «＋» ----------

const ask = (script) => run(`osascript ${script.map((line) => `-e '${line}'`).join(" ")}`).then((out) => out.trim());

const askText = (title, prompt) =>
  ask([`display dialog "${prompt}" default answer "" with title "${title}" buttons {"Отмена", "Готово"} default button 2`, "text returned of result"]);

const choose = (title, items) =>
  ask([`choose from list {${items.map((i) => `"${i}"`).join(", ")}} with title "${title}" default items {"${items[0]}"}`]).then((out) => {
    if (!out || out === "false") throw new Error("cancel");
    return out;
  });

const KINDS = { Репетитор: "tutor", "Засечка под личные дела": "me", "Доп. урок": "lesson", "Доп. дело": "task" };

const send = (path, body) =>
  fetch(`${BOT}/${path}`, { method: "POST", headers: { "Content-Type": "text/plain" }, body: JSON.stringify(body) }).then((r) => r.json());

const addEvent = async (dispatch) => {
  try {
    const text = await askText("Новое в расписании", "Что и когда? Например: в пятницу 19:00–20:00 китайский");
    if (!text) return;
    const kind = KINDS[await choose("Что это?", Object.keys(KINDS))];
    const once = (await choose("Разово или каждую неделю?", ["Разово", "Каждую неделю"])) === "Разово";
    dispatch({ type: "BUSY", text: "Добавляю…" });
    const res = await send("add", { text, kind, once });
    if (res.error) return dispatch({ type: "ERROR", error: res.error });
    load(dispatch);
  } catch (e) {
    /* нажали «Отмена» — ничего не делаем */
  }
};

const addNote = async (dispatch) => {
  try {
    const text = await askText("Новая заметка", "Что записать? Как сообщение боту — срок можно словами");
    if (!text) return;
    dispatch({ type: "BUSY", text: "Сохраняю…" });
    const res = await send("add", { text, kind: "note" });
    if (res.error) return dispatch({ type: "ERROR", error: res.error });
    load(dispatch);
  } catch (e) {}
};

const close = async (dispatch, id) => {
  dispatch({ type: "BUSY", text: "Закрываю…" });
  const res = await send("done", { id }).catch(() => ({ error: "Нет связи с ботом" }));
  if (res.error) return dispatch({ type: "ERROR", error: res.error });
  load(dispatch);
};

const shift = (dispatch, data, days) => {
  const d = new Date(`${data.date}T12:00:00`);
  d.setDate(d.getDate() + days);
  shownDate = d.toISOString().slice(0, 10);
  load(dispatch);
};

const openLink = (link) => {
  if (/^https:\/\/[^\s'"]+$/.test(link)) run(`open '${link}'`);
};

// ---------- оформление: палитра «Моего расписания» из Figma ----------

const COLORS = {
  lesson: ["#D5D4FF", "#9B4DDB"],
  tutor: ["#FFE1FD", "#9B4DDB"],
  other: ["#F5FFCB", "#A37E2F"],
  event: ["#FFF4E6", "#A37E2F"],
  routine: ["#F2F2F6", "#8A8A95"],
  note: ["#F2F2F6", "#8A8A95"],
};
const URGENCY = { fire: ["горит", "#E5534B"], soon: ["скоро", "#1F1F24"], later: ["потом", "#8A8A95"] };

export const className = css`
  top: 24px;
  right: 24px;
  width: 360px;
  font-family: "PT Sans", -apple-system, sans-serif;
  color: #1f1f24;
  -webkit-font-smoothing: antialiased;
`;

const card = css`
  background: #ffffff;
  border-radius: 18px;
  box-shadow: 0 8px 30px rgba(31, 31, 36, 0.14);
  padding: 16px 16px 12px;
  margin-bottom: 12px;
`;
const head = css`
  display: flex;
  align-items: center;
  background: #fff4e6;
  border-radius: 8px;
  padding: 6px 10px;
  margin-bottom: 10px;
  h1 {
    font-family: "Paytone One", "PT Sans", sans-serif;
    font-size: 17px;
    font-weight: normal;
    margin: 0;
    flex: 1;
    letter-spacing: -0.02em;
  }
  small {
    font-size: 11px;
    color: #8a8a95;
    margin-right: 8px;
  }
`;
const btn = css`
  border: none;
  background: transparent;
  font-size: 16px;
  cursor: pointer;
  color: #1f1f24;
  padding: 0 4px;
  &:hover {
    color: #9b4ddb;
  }
`;
const row = css`
  display: flex;
  gap: 10px;
  margin-bottom: 6px;
  .time {
    width: 42px;
    font-size: 12px;
    font-weight: 700;
    padding-top: 6px;
    text-align: right;
    color: #1f1f24;
  }
  .box {
    flex: 1;
    border-radius: 6px;
    padding: 5px 9px 6px;
    border: 1px solid;
  }
  .title {
    font-size: 13.5px;
    font-weight: 700;
  }
  .meta {
    font-size: 11px;
    color: #5a5a66;
  }
  .badge {
    float: right;
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    margin-left: 6px;
  }
  .join {
    float: right;
    font-size: 11px;
    border: 1px solid #9b4ddb;
    border-radius: 10px;
    padding: 0 8px;
    cursor: pointer;
    background: #fff;
  }
`;
const gap = css`
  margin: 2px 0 8px 52px;
  font-size: 11px;
  color: #8a8a95;
  border-left: 2px dashed #e6e6eb;
  padding-left: 8px;
`;
const plan = css`
  display: flex;
  gap: 8px;
  padding: 5px 0;
  border-top: 1px solid #f2f2f6;
  .check {
    width: 14px;
    height: 14px;
    border: 1.5px solid #1f1f24;
    border-radius: 3px;
    margin-top: 2px;
    cursor: pointer;
    flex: none;
    &:hover {
      background: #d5d4ff;
    }
  }
  .t {
    font-size: 13px;
    font-weight: 700;
  }
  .w {
    font-size: 11px;
    color: #8a8a95;
  }
  .step {
    display: flex;
    gap: 6px;
    font-size: 12px;
    margin-top: 3px;
  }
  .step .check {
    width: 11px;
    height: 11px;
  }
`;
const note = css`
  font-size: 11px;
  color: #8a8a95;
  margin-top: 6px;
`;

const minutes = (hhmm) => (hhmm ? Number(hhmm.slice(0, 2)) * 60 + Number(hhmm.slice(3, 5)) : 0);
const pause = (m) => (m >= 60 ? `${Math.floor(m / 60)} ч${m % 60 ? ` ${m % 60} мин` : ""}` : `${m} мин`);

const Day = ({ data, dispatch }) => {
  const now = minutes(data.now);
  const nextIndex = data.today ? data.items.findIndex((i) => minutes(i.start) > now) : -1;
  const out = [];
  data.items.forEach((item, k) => {
    const prev = data.items[k - 1];
    if (prev && prev.end && item.start && minutes(item.start) - minutes(prev.end) >= 30) {
      out.push(<div className={gap} key={`gap${k}`}>окно {pause(minutes(item.start) - minutes(prev.end))}</div>);
    }
    const [fill, border] = COLORS[item.kind] || COLORS.note;
    const current = data.today && item.start && item.end && minutes(item.start) <= now && now < minutes(item.end);
    const meta = [item.end ? `до ${item.end}` : "", item.online ? "онлайн" : "", item.teacher, item.once ? "разово" : ""].filter(Boolean).join(" · ");
    out.push(
      <div className={row} key={k}>
        <div className="time">{item.start || "весь день"}</div>
        <div className="box" style={{ background: fill, borderColor: border, borderWidth: current ? 2 : 1 }}>
          {current && <span className="badge" style={{ color: "#9B4DDB" }}>сейчас</span>}
          {k === nextIndex && !current && <span className="badge" style={{ color: "#8A8A95" }}>дальше</span>}
          {item.link && <span className="join" onClick={() => openLink(item.link)}>войти</span>}
          <div className="title">{item.title}</div>
          {meta && <div className="meta">{meta}</div>}
        </div>
      </div>
    );
  });
  return out.length ? out : <div className={note}>Свободный день 🌿</div>;
};

const Plans = ({ plans, all, dispatch }) => {
  const shown = all ? plans : plans.slice(0, 8);
  return (
    <div>
      {shown.map((p) => {
        const [label, color] = URGENCY[p.urgency];
        return (
          <div className={plan} key={p.id}>
            <div className="check" title="Закрыть" onClick={() => close(dispatch, p.id)} />
            <div style={{ flex: 1 }}>
              <div className="t">
                {p.title} <span style={{ color, fontSize: 10, fontWeight: 700, textTransform: "uppercase" }}>{label}</span>
              </div>
              <div className="w">{[p.when && `до ${p.when}`, p.where].filter(Boolean).join(" · ")}</div>
              {p.steps.map((s) => (
                <div className="step" key={s.id}>
                  <div className="check" title="Закрыть" onClick={() => close(dispatch, s.id)} />
                  {s.title}
                </div>
              ))}
            </div>
          </div>
        );
      })}
      {plans.length > 8 && (
        <div className={note} style={{ cursor: "pointer" }} onClick={() => dispatch({ type: "TOGGLE_PLANS" })}>
          {all ? "свернуть" : `ещё ${plans.length - 8}…`}
        </div>
      )}
    </div>
  );
};

export const render = ({ data, error, busy, allPlans }, dispatch) => (
  <div>
    <style>{"@import url('https://fonts.googleapis.com/css2?family=Paytone+One&family=PT+Sans:wght@400;700&display=swap');"}</style>
    <div className={card}>
      <div className={head}>
        <button className={btn} onClick={() => data && shift(dispatch, data, -1)}>‹</button>
        <h1>{data ? data.title : "Мой день"}</h1>
        {data && data.relative && <small>{data.relative}</small>}
        <button className={btn} onClick={() => data && shift(dispatch, data, 1)}>›</button>
        <button className={btn} title="Добавить в расписание" onClick={() => addEvent(dispatch)}>＋</button>
      </div>
      {data ? <Day data={data} dispatch={dispatch} /> : <div className={note}>Загружаю…</div>}
      {data && !data.today && (
        <div className={note} style={{ cursor: "pointer" }} onClick={() => { shownDate = ""; load(dispatch); }}>
          ↩︎ к сегодня
        </div>
      )}
    </div>
    <div className={card}>
      <div className={head}>
        <h1>Plans</h1>
        {data && <small>{data.plans.length}</small>}
        <button className={btn} title="Новая заметка" onClick={() => addNote(dispatch)}>＋</button>
      </div>
      {data ? <Plans plans={data.plans} all={allPlans} dispatch={dispatch} /> : null}
      {(busy || error) && <div className={note}>{busy || error}</div>}
    </div>
  </div>
);
