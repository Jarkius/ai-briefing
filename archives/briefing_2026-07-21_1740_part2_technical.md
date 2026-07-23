# AI Briefing Part 2: Technical & Community — Tuesday, July 21, 2026

### 🧪 Research radar (arXiv — brief)
Practical signals for professionals, not just researchers:
- **PlanFlip** — attacks multi-agent AI systems by injecting malicious prompts during the *planning* phase, before executor/critic agents run. *Takeaway: secure the planning step, not just the final action.*
- **Deterministic Replay for AI Agents** — a method to reliably reproduce agent runs despite model randomness and shifting APIs. *Takeaway: reproducibility tooling is maturing — key for debugging and audits.*
- **Rater State Bias in RLHF** — human feedback used to train models can reflect the *rater's* mood/state, not just output quality. *Takeaway: "aligned to human preference" carries hidden bias.*
- **LLM Unlearning for Cyber Defense (survey)** — models can't easily "forget," creating privacy and security risks. *Takeaway: assume data fed to a model may be hard to remove.*
- **Some LLMs show consistent risk attitudes** — models translate perceived risk into action in stable, measurable ways. *Takeaway: relevant if you let AI make judgment calls.*
📱 Social post: This week's AI research in one line: attackers can hijack agents at the *planning* stage (PlanFlip), RLHF encodes rater mood as "preference," and models can't easily unlearn your data. Secure and audit accordingly. #AISecurity #AIResearch #LLMs
*Source: arXiv (RSS)*

### 🛠️ Tools & learning resources (GitHub trending — brief)
Great starting points for building AI literacy and skills:
- **microsoft/generative-ai-for-beginners** (113k⭐) and **microsoft/ML-For-Beginners** (88k⭐) — structured free curricula.
- **dair-ai/Prompt-Engineering-Guide** (77k⭐) and **f/prompts.chat** (166k⭐) — prompt-craft references.
- **rasbt/LLMs-from-scratch** (99k⭐) — build an LLM to understand one.
- **OpenHands** (81k⭐) and **AutoGPT** (186k⭐) — autonomous coding/agent frameworks.
- **usestrix/strix** (43k⭐) — AI-driven security tooling. **janhq/jan** (44k⭐) — local, private chat.
**Key takeaway:** Point new learners at Microsoft's free "for beginners" courses and the DAIR prompt guide before buying any training.
📱 Social post: Free, high-quality AI upskilling on GitHub right now: Microsoft's "Generative AI for Beginners," DAIR's Prompt Engineering Guide, and "LLMs from Scratch." Start here before paying for a course. #AILiteracy #PromptEngineering #LearnAI
*Source: GitHub trending*

---

*Not covered (outside AI scope or off-topic in the raw feed): Airport Simulator, the Egyptian tomb discovery, Firefox 153, the HP cartelization fine, Sheetz's VMware migration, and X's Android app relaunch. Unverified/rumor items were flagged above. Robotics releases (Xiaomi-Robotics-1, Grabette, NVIDIA infrastructure blogs, Cosmos 3 Edge) are noted here for awareness but were lower-priority for this audience.*

Want me to expand any cluster into full write-ups, turn the social posts into a scheduled thread, or tailor a version specifically for educators or for your security team?

---

Here's the briefing. One transparency note up front: the raw feed provided story titles and scores but **no direct URLs**, so each `[Source]` link points to the publishing outlet's newsroom/section (best-effort attribution) rather than the exact article. Claims that the data itself flags as speculative are marked as **rumour/reported**.