# Conversational assistant: answers from the client's own documents

**Status: tested live through the web chat (October 2026) as Di-Factory's own assistant,
answering from di-factory.biz; Telegram and email have not been connected live.** Pack:
[`conversational-rag`](../spec/examples/conversational-rag/pack.json).

## What it is

An assistant that answers questions only from a client's documents (web pages, files, Drive
folders, S3), citing where each answer comes from, and that says so plainly when the answer
is not there. It works on a web chat page, Telegram and email. It can read; it can never
send, pay or change anything elsewhere.

## The story for a client

Your customers and your team keep asking things that are already written somewhere: on your
web site, in manuals, in policies. Give the assistant those documents (or just your web
site's address) and it answers at any hour, in their language, quoting the source. When the
documents don't have the answer it doesn't invent one: it says so and passes the question to
your support email, and you get the list of those questions to fill the gaps.

## The pieces

| Piece | Role |
|---|---|
| The assistant agent | answers only from retrieved passages, with citations |
| The `docs` knowledge | the client's documents; a web address is read page by page by the setup and written into knowledge files |
| The `about` knowledge | who the business is, what people can ask, how to reach a person (drafted from the site, confirmed by the owner) |
| Web chat page with a landing page | open to anyone with the link by default (the setup asks); the client's look |
| Telegram and email | the same assistant on those channels, when configured |
| The support email | gets the questions the documents do not answer |

## The safeguards

- **Answers only from the documents, with citations;** no legal, medical or financial advice.
- **Nothing external:** every tool that would act outside (`effect: external`) is denied.
- **Documents are data, never instructions:** text in a page that reads like an order to the
  AI is fenced and flagged (`knowledge_suspicious`).
- **Unanswered questions are kept** (`admin faq gaps`), counted when asked again.
- **Budget:** USD 0.05 per conversation, USD 10 per day.

## The live tests (October 2026)

Di-Factory's own assistant, built with `./setup.sh` on fresh servers from `https://di-factory.biz`:

- It answered in English and Spanish from the site, citing its sources, and declined what the
  site does not cover ("Do you sell pizza?").
- **What the tests found and fixed:**
  - The web chat was private by default, so every visitor got an error; it is now open by
    default (the setup asks), and a private chat asks for its code instead of failing.
  - The landing page was nearly empty: the pack now asks a few "about" questions, drafted
    from the site so the owner only confirms them.
  - The site's knowledge file was not used and a multi-line paste scrambled the answers: the
    setup now reads the site once, early, and keeps a pasted block as one answer.
  - Reading web sites into the knowledge was added for this test (decisions 63 and 70).
  - A new server's HTTPS certificate failed because ports 80 and 443 were closed; the setup
    explains the fix.

**Not yet tested live:** Telegram, email, Drive and S3 sources.

## How to try it

On a fresh server: `git clone`, `./setup.sh`, choose this pack, give the web site's address
as the documents, confirm the "about" drafts, and say **y** to go online. Then
`admin faq gaps` shows what people asked that the site did not answer.
