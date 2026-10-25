// Виджет «Мой день» для iPhone — бесплатное приложение Scriptable (App Store).
// Как поставить: открой Scriptable → «＋» → вставь весь этот текст → назови «Мой день» → на экране «Домой»
// добавь виджет Scriptable (большой или средний) → удерживай его → «Изменить виджет» → Script: «Мой день».
// Средний — ближайшие занятия; большой — день и планы. Нажатие открывает бота в Telegram, занятие со ссылкой — вход.
// Адрес бота ниже секретный: не пересылай этот текст.

const BOT = "__BOT_URL__";
const TELEGRAM = "https://t.me/__BOT_USERNAME__";
const dark = Device.isUsingDarkAppearance();

// палитра «Моего расписания» из Figma; в тёмной теме — тёмные карточки со светлым текстом
const COLORS = dark
  ? { lesson: ["#3A3666", "#B9A6F2"], tutor: ["#5A2E58", "#F0A6E8"], other: ["#3F4A1E", "#D8E68A"], event: ["#4A3A22", "#E8C48A"], routine: ["#2E2E36", "#9A9AA6"], note: ["#2E2E36", "#9A9AA6"] }
  : { lesson: ["#D5D4FF", "#9B4DDB"], tutor: ["#FFE1FD", "#9B4DDB"], other: ["#F5FFCB", "#A37E2F"], event: ["#FFF4E6", "#A37E2F"], routine: ["#F2F2F6", "#8A8A95"], note: ["#F2F2F6", "#8A8A95"] };
const TEXT = new Color(dark ? "#F7F5F2" : "#1F1F24");
const MUTED = new Color(dark ? "#F7F5F2" : "#1F1F24", 0.6);
const URGENCY = { fire: ["горит", "#E5534B"], soon: ["скоро", dark ? "#F7F5F2" : "#1F1F24"], later: ["потом", "#8A8A95"] };

const minutes = (t) => (t ? Number(t.slice(0, 2)) * 60 + Number(t.slice(3, 5)) : 0);

async function load() {
  try {
    return await new Request(`${BOT}/day`).loadJSON();
  } catch (e) {
    return null;
  }
}

function text(stack, value, size, color = TEXT, bold = false) {
  const t = stack.addText(value);
  t.font = bold ? Font.boldSystemFont(size) : Font.systemFont(size);
  t.textColor = color;
  t.lineLimit = 1;
  return t;
}

function lesson(w, item, current) {
  const row = w.addStack();
  row.layoutHorizontally();
  row.centerAlignContent();
  const time = row.addStack();
  time.size = new Size(40, 0);
  text(time, item.start || "весь", 11, TEXT, true);
  row.addSpacer(6);
  const [fill, border] = COLORS[item.kind] || COLORS.note;
  const box = row.addStack();
  box.layoutVertically();
  box.backgroundColor = new Color(fill);
  box.cornerRadius = 6;
  box.borderColor = new Color(border);
  box.borderWidth = current ? 2 : 1;
  box.setPadding(3, 7, 3, 7);
  const head = box.addStack();
  text(head, item.title, 12, TEXT, true);
  head.addSpacer();
  if (current) text(head, "сейчас", 9, new Color(border), true);
  const meta = [item.end ? `до ${item.end}` : "", item.online ? "онлайн" : "", item.teacher].filter(Boolean).join(" · ");
  if (meta) text(box, meta, 10, MUTED);
  if (item.link) box.url = item.link;
  w.addSpacer(4);
}

async function main() {
  const data = await load();
  const family = config.widgetFamily || "large";
  const w = new ListWidget();
  w.backgroundColor = new Color(dark ? "#1C1B22" : "#FFFAF4");
  w.setPadding(12, 12, 12, 12);
  w.url = TELEGRAM;
  w.refreshAfterDate = new Date(Date.now() + 15 * 60 * 1000);
  if (!data) {
    text(w, "Нет связи с ботом", 13, TEXT, true);
    return w;
  }
  const head = w.addStack();
  head.centerAlignContent();
  text(head, data.title, 15, TEXT, true);
  head.addSpacer();
  if (data.relative) text(head, data.relative, 11, MUTED);
  w.addSpacer(8);

  const now = minutes(data.now);
  // сегодня — только то, что ещё не кончилось
  let items = data.today ? data.items.filter((i) => !i.end || minutes(i.end) > now) : data.items;
  const limit = family === "large" ? 6 : family === "medium" ? 3 : 2;
  if (!items.length) text(w, "Свободно 🌿", 12, MUTED);
  items.slice(0, limit).forEach((i) => lesson(w, i, data.today && minutes(i.start) <= now && now < minutes(i.end)));
  if (items.length > limit) text(w, `и ещё ${items.length - limit}…`, 10, MUTED);

  if (family === "large" && data.plans.length) {
    w.addSpacer(8);
    text(w, "Plans", 13, TEXT, true);
    w.addSpacer(4);
    data.plans.slice(0, 4).forEach((p) => {
      const row = w.addStack();
      row.centerAlignContent();
      text(row, "☐ ", 12, TEXT);
      text(row, p.title, 12, TEXT, true);
      row.addSpacer(4);
      const [label, color] = URGENCY[p.urgency];
      text(row, label, 9, new Color(color), true);
      w.addSpacer(3);
    });
  }
  w.addSpacer();
  return w;
}

const widget = await main();
if (config.runsInWidget) Script.setWidget(widget);
else await widget.presentLarge();
Script.complete();
