"""
Block-based template engine.

A Template is a named, ordered list of blocks:
    [{"key": "greeting", "enabled": true, "content": "Hi {{ recipient.first_name }},"}, ...]

Each block's `content` is plain text containing {{ dotted.variable }} placeholders,
resolved with Jinja2's sandboxed environment against a nested variable context:

    recipient.first_name / full_name / role
    company.name / industry / website
    sender.name / university / degree / graduation_year / portfolio / linkedin
    campaign.type / internship_year / placement_year
    custom.<anything the campaign/recipient defines>

Rendering produces THREE outputs from the same source:
    - plain_text : blocks joined with blank lines (the true fallback)
    - html       : the same blocks wrapped in a professional, inline-CSS,
                   table-based HTML shell (Gmail + Outlook safe)
    - subject    : the resolved subject line

Design choice: blocks are stored and edited as plain text, never as raw HTML.
This keeps the "looks like a real personal email, not a marketing blast"
requirement structurally guaranteed — there is no rich-text/marketing-style
block type, only paragraphs, so the model can't drift into flashy layouts.
"""
import html as _html
from jinja2 import Environment, StrictUndefined, TemplateError

BLOCK_REGISTRY = {
    "greeting":               "Opening salutation",
    "personal_opening":       "Personal opening line / hook",
    "why_company":            "Why this company specifically",
    "profile":                "Sender's profile / background",
    "skills":                 "Relevant skills",
    "projects":               "Relevant projects",
    "internship_inquiry":     "2027-style internship inquiry",
    "graduate_inquiry":       "Graduate / placement hiring inquiry",
    "campus_inquiry":         "Campus / institutional hiring inquiry",
    "portfolio":              "Portfolio link mention",
    "resume":                 "Resume mention (attachment reference)",
    "personal_note":          "Free-form personal note",
    "cta":                    "Call to action",
    "signature":              "Signature block",
}

DEFAULT_BLOCK_ORDER = [
    "greeting", "personal_opening", "why_company", "profile", "skills",
    "projects", "internship_inquiry", "graduate_inquiry", "campus_inquiry",
    "portfolio", "resume", "personal_note", "cta", "signature",
]

# A safe subset of Jinja2 — no attribute access beyond dict lookups needed,
# StrictUndefined so a typo'd variable raises instead of silently rendering "".
_env = Environment(undefined=StrictUndefined, autoescape=False)


class TemplateRenderError(Exception):
    pass


def _flatten_missing_var_name(err: TemplateError) -> str:
    msg = str(err)
    return msg


def render_text(source: str, context: dict) -> str:
    try:
        tmpl = _env.from_string(source or "")
        return tmpl.render(**context)
    except TemplateError as e:
        raise TemplateRenderError(f"Unknown variable in template: {_flatten_missing_var_name(e)}")


def build_context(recipient=None, company=None, sender=None, campaign=None, custom=None):
    return {
        "recipient": recipient or {},
        "company":   company or {},
        "sender":    sender or {},
        "campaign":  campaign or {},
        "custom":    custom or {},
    }


def render_blocks(blocks: list, context: dict) -> list:
    """Returns list of rendered plain-text paragraphs for enabled blocks, in order."""
    paragraphs = []
    for block in blocks:
        if not block.get("enabled", True):
            continue
        content = block.get("content", "").strip()
        if not content:
            continue
        paragraphs.append(render_text(content, context))
    return paragraphs


def render_template(template: dict, context: dict) -> dict:
    """
    template = {"subject_template": "...", "blocks": [...]}
    Returns {"subject": str, "plain_text": str, "html": str}
    """
    subject = render_text(template.get("subject_template", ""), context)
    paragraphs = render_blocks(template.get("blocks", []), context)
    plain_text = "\n\n".join(paragraphs)
    html_body = wrap_html(paragraphs)
    return {"subject": subject, "plain_text": plain_text, "html": html_body}


def wrap_html(paragraphs: list) -> str:
    """
    Professional, personal-looking HTML email — NOT a marketing template.
    Table-based layout, inline CSS, system fonts, single column, no images,
    no banner/header graphics. This is deliberate: it should read like an
    email a person typed, not a newsletter.
    """
    body_html = "\n".join(
        f'<p style="margin:0 0 16px 0;padding:0;font-family:Arial,Helvetica,sans-serif;'
        f'font-size:14px;line-height:1.6;color:#1a1a1a;">{_html.escape(p).replace(chr(10), "<br>")}</p>'
        for p in paragraphs
    )
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin:0;padding:0;background-color:#ffffff;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#ffffff;">
  <tr>
    <td align="center" style="padding:24px 16px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;">
        <tr>
          <td style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.6;color:#1a1a1a;">
            {body_html}
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
</body>
</html>"""


CAMPAIGN_TYPE_PRESETS = {
    "internship_2027": {
        "greeting", "personal_opening", "profile", "skills", "projects",
        "internship_inquiry", "resume", "cta", "signature",
    },
    "graduate_2028": {
        "greeting", "personal_opening", "profile", "skills", "projects",
        "graduate_inquiry", "resume", "cta", "signature",
    },
    "recruiter_networking": {
        "greeting", "personal_opening", "why_company", "profile",
        "personal_note", "portfolio", "cta", "signature",
    },
    "campus_outreach": {
        "greeting", "personal_opening", "why_company", "campus_inquiry",
        "cta", "signature",
    },
    "custom": set(DEFAULT_BLOCK_ORDER),
}


def default_blocks_for(campaign_type: str) -> list:
    enabled_keys = CAMPAIGN_TYPE_PRESETS.get(campaign_type, CAMPAIGN_TYPE_PRESETS["custom"])
    return [
        {"key": key, "enabled": key in enabled_keys, "content": ""}
        for key in DEFAULT_BLOCK_ORDER
    ]
