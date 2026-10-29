# Appointment agent: a virtual receptionist for clinics and practices

**Status: tested live through the web chat (October 2026), on several fresh servers; the
WhatsApp channel and the Google Calendar connection have not been connected live yet.**
Pack: [`pyme-appointment-agent`](../spec/examples/pyme-appointment-agent/pack.json).

## What it is

A receptionist for a small business with appointments (a clinic, a dental practice, a
plumber...). It answers patients' questions from the business's own FAQ, confirms,
reschedules and cancels appointments, and reminds patients before their appointment. It
works over WhatsApp/SMS and a web chat page with the business's own look, and hands a
conversation to a person whenever it should.

## The story for a client

Your phone rings all day for the same questions (hours, prices, where you are) and to move
appointments; and patients forget theirs. The assistant answers those questions on WhatsApp
and on your web page, at any hour, only from what you told it; it reminds every patient the
day before and handles the "can I move it to Thursday?". Anything medical, urgent or a
complaint goes straight to you. It never invents an answer: what your FAQ doesn't cover, it
says so and passes on, and you see the list of those questions to add the answers.

## The pieces

| Piece | Role |
|---|---|
| The receptionist agent | talks with patients: FAQ, confirm, reschedule, cancel |
| The business's FAQ (knowledge) | its only source of answers; small FAQs are read whole |
| Google Calendar (one per practitioner) | free slots, moving and cancelling appointments |
| WhatsApp/SMS gateway (the client's own account) | messages and approved templates for reminders |
| Web chat page with a landing page | the same receptionist on the web, in the business's colours and logo |
| The reminder workflow | N hours before each appointment: a template, wait up to 12 h for a reply, the agent handles it, or a nudge if there was none |
| The inbox (`admin inbox`) | conversations handed to a person, approvals |

## The safeguards

- **Answers only from the FAQ;** a question it cannot answer is said so, handed over and
  recorded (`admin faq gaps`).
- **Medical advice, emergencies and complaints** always go to a person.
- **Cancelling an appointment asks a person;** deleting calendar events is denied.
- **Personal data** (names, phones, email, CURP, health) is tokenized: the model never sees
  it; only the patient's first name appears in replies.
- **Consent:** reminders only to patients who did not opt out; `BAJA`, `STOP` or `ALTO`
  stops them.
- **Budget:** USD 0.20 per conversation, USD 5 per day.
- **Every rebuild replays real conversations** before going online (a reply that got worse
  stops it).

## The live tests (September–October 2026)

Set up from scratch several times on new EC2 servers with `git clone` and `./setup.sh`
(the guided setup: key, questionnaire, brand look, build, a real test question, online with
HTTPS at `<ip>.sslip.io`). Tested through the web chat:

- A plumber: "¿Qué servicios ofrecen y cuánto cuesta la consulta?" was answered in Spanish
  from the FAQ (consultation price, refund policy, address, hours) with an offer to book; an
  urgent water leak was handed straight to a person.
- The business's look (colours given in words: "dark blue and white") on the landing page
  and the chat.
- Today a clinic (`clinica`) runs on the current server.

**What the tests found and fixed** (decisions 65–75 and others): template files lost when an
instance was copied (`spec copy`), API keys pasted twice or without a workspace (the setup
now refuses them), a client that could not start shown as a 502 (the setup now refuses to
put it online), the web chat page, brand looks from any material, FAQs read whole instead of
by keyword, published contact details shown to customers, the handover to the client's own
Claude, and the guided `./setup.sh` for a fresh server.

**Not yet tested live:** WhatsApp (needs the client's gateway account and approved
templates), Google Calendar (needs a service account) and therefore the reminder workflow.

## How to try it

On a fresh server: `git clone`, `./setup.sh`, choose this pack, answer the questions, say
**y** to go online, and open `https://<ip-with-dashes>.sslip.io`. See
[`docs/GETTING_STARTED.md`](../GETTING_STARTED.md).
