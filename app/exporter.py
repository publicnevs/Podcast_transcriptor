import json


def export_txt(episode_title, podcast_title, pub_date, transcript, summary, takeaways):
    lines = [
        f"PODCAST: {podcast_title}",
        f"EPISODE: {episode_title}",
        f"DATUM: {pub_date}",
        "=" * 70,
        "",
    ]
    if summary:
        lines += ["ZUSAMMENFASSUNG:", summary, ""]
    if takeaways:
        lines += ["KEY TAKEAWAYS:"]
        for t in takeaways:
            lines.append(f"• {t}")
        lines.append("")
    lines += ["TRANSKRIPT:", "=" * 70, "", transcript or ""]
    return "\n".join(lines)


def export_markdown(episode_title, podcast_title, pub_date, transcript, summary, takeaways, chapters):
    lines = [
        f"# {episode_title}",
        f"**Podcast:** {podcast_title}  ",
        f"**Datum:** {pub_date}",
        "",
    ]
    if summary:
        lines += ["## Zusammenfassung", summary, ""]
    if takeaways:
        lines += ["## Key Takeaways"]
        for t in takeaways:
            lines.append(f"- {t}")
        lines.append("")
    if chapters:
        lines += ["## Kapitel"]
        for ch in chapters:
            lines.append(f"- **{ch.get('start_time', '')}** — {ch.get('title', '')}")
            if ch.get("summary"):
                lines.append(f"  {ch['summary']}")
        lines.append("")
    lines += ["## Transkript", "", transcript or ""]
    return "\n".join(lines)


def export_ai_copy(episode_title, podcast_title, pub_date, transcript, summary, takeaways):
    lines = [
        "<podcast_transcript>",
        "<metadata>",
        f"  <show>{podcast_title}</show>",
        f"  <episode>{episode_title}</episode>",
        f"  <date>{pub_date}</date>",
        "</metadata>",
    ]
    if summary:
        lines += [f"<summary>\n{summary}\n</summary>"]
    if takeaways:
        lines += ["<key_points>"]
        for t in takeaways:
            lines.append(f"  <point>{t}</point>")
        lines.append("</key_points>")
    lines += ["<transcript>", transcript or "", "</transcript>", "</podcast_transcript>"]
    return "\n".join(lines)


def export_chat_markdown(messages):
    """Render a RAG chat conversation (list of {role, content, sources}) as
    Markdown, including cited sources as deep links into the episodes."""
    from datetime import datetime
    lines = [
        "# Frag deine Bibliothek — Gespräch",
        f"*Exportiert mit PodScribe am {datetime.now().strftime('%d.%m.%Y %H:%M')}*",
        "",
        "---",
        "",
    ]
    for m in messages:
        role = m.get("role")
        content = m.get("content", "")
        if role == "user":
            lines += [f"## ❓ {content}", ""]
        else:
            lines += [content, ""]
            sources = m.get("sources") or []
            if sources:
                lines += ["**Quellen:**"]
                for s in sources:
                    t = s.get("start_time") or "00:00:00"
                    ep_id = s.get("episode_id")
                    label = f"{s.get('episode_title', '')} ({s.get('podcast_title', '')}) — {t}"
                    link = f"/episode/{ep_id}?t={t}" if ep_id else ""
                    ref = s.get("ref", "")
                    lines.append(f"- [{ref}] [{label}]({link})" if link else f"- [{ref}] {label}")
                lines.append("")
            lines += ["---", ""]
    return "\n".join(lines)


def bulk_export_markdown(podcast_title, episodes):
    lines = [
        f"# {podcast_title} — Alle Transkripte",
        "*Exportiert mit PodScribe*",
        "",
        "---",
        "",
    ]
    for ep in episodes:
        lines += [f"## {ep.get('title', 'Unbekannte Folge')}",
                  f"**Datum:** {ep.get('pub_date', '')}",
                  ""]
        if ep.get("summary"):
            lines += [f"**Zusammenfassung:** {ep['summary']}", ""]
        if ep.get("transcript"):
            lines += [ep["transcript"], ""]
        lines += ["---", ""]
    return "\n".join(lines)
