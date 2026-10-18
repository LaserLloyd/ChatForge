# Prompts to try on the first run

Ten things to paste into ChatForge to see what it does, each with what to expect from the starter model
(`Qwen2.5-1.5B-Instruct`, 0.94 GB). It is a small model: it is quick and polite, and it makes mistakes, so the
prompts below lean on what it is good at (short rewrites, explanations, a tool doing the real work) and say where
it is shaky. Numbers measured on a CPU-only PC (no NPU) are around 45 tokens per second once loaded; the first
message after a start waits a few seconds while the model loads.

The local model is offered five tools: web search, page fetch, weather, date and time, calculator. A StudioForge
or cloud model gets all nine.

## Plain writing (no tools, any machine)

1. **"Write a two-sentence reply politely declining a meeting invitation for Thursday."**
   A short, courteous reply. The easiest proof that everything works.
2. **"Explain what an NPU is to a ten-year-old, in three sentences."**
   A plain-language explanation. Try *Make it shorter* from the quick actions afterwards.
3. **Paste a paragraph of your own, then press the *Proof this* quick action.**
   The corrected text in a copyable block, then a list of what changed.
4. **Paste a long email, then *Summarize*.**
   A few bullet points. A 4096-token window means very long text is cut: see *Long conversations* in the README.
5. **"Give me five names for a hiking club, one per line."**
   Quick brainstorming. Press **Regenerate** for five more.

## Tools (the model looks things up, and a chip in the reply says which tool ran)

6. **"What time is it now?"**
   Calls `current_datetime` and answers from your PC's clock.
7. **"Use the calculator tool: what is 18% of 2,430?"** (answer: 437.4)
   Calls `calculator`. Say "use the calculator" for sums: left to itself, a 1.5B model sometimes does the
   arithmetic in its head and gets it wrong (in testing it once answered 455.4 to this question).
8. **"What is the weather in Lisbon?"**
   Calls `weather` (Open-Meteo, no key). Needs internet. Set *Your location* in Settings > General and then
   "what is the weather like here?" works too.
9. **"Search the web for the latest OpenVINO Model Server release and name it."**
   Calls `web_search` (DuckDuckGo) and answers from the results, with a source. Needs internet; small models
   can still misread a result, so check the source link.
10. **"Fetch https://example.com and tell me what the page says."**
    Calls `fetch_url`. Say "fetch" explicitly: "summarise <url>" alone often makes a small model guess.

## Need more than the small model gives?

- **A report, a spreadsheet or a slide deck:** ask a StudioForge or cloud model, for example *"Make a one-page
  meeting summary as a Word file with these notes: ..."*. It saves a `.docx`, `.xlsx` or `.pptx` in
  `Documents\ChatForge` and shows an Open button. The local model does not get `create_document`.
- **Pictures:** attach one with the paperclip. Only vision models read the picture itself (some StudioForge and
  cloud models); for the others ChatForge sends the text Windows can read in it.
- **Hard questions:** switch the model in the header chip. See *Providers* in the README; StudioForge on another
  PC needs an API key (`studioforge keys create` on that PC).

If a prompt does nothing or errors, Settings > Logs shows why, and `chatforge doctor` checks the machine.
