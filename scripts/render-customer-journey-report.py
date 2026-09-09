"""Render the local audit Markdown as a self-contained, offline HTML report."""
import base64
import html
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "artifacts/api-server/docs/customer-journey-audit.md"
OUTPUT = ROOT / "artifacts/api-server/docs/customer-journey-evidence/report.html"


def inline(text):
    text = html.escape(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    return text


def render(markdown):
    output = []
    paragraph = []
    table = []
    code = None

    def flush():
        if paragraph:
            output.append("<p>" + inline(" ".join(paragraph)) + "</p>")
            paragraph.clear()
        if table:
            output.append('<div class="table-scroll"><table>')
            for index, row in enumerate(table):
                cell = "th" if index == 0 else "td"
                output.append("<tr>" + "".join(
                    f"<{cell}>{inline(value.strip())}</{cell}>"
                    for value in row.strip("|").split("|")
                ) + "</tr>")
            output.append("</table></div>")
            table.clear()

    for line in markdown.splitlines():
        if line.startswith("```"):
            if code is None:
                flush()
                code = []
            else:
                output.append("<pre><code>" + html.escape("\n".join(code))
                              + "</code></pre>")
                code = None
            continue
        if code is not None:
            code.append(line)
            continue
        if line.startswith("|"):
            if not re.fullmatch(r"[|\s:-]+", line):
                table.append(line)
            continue
        if line.startswith("#"):
            flush()
            level = len(line) - len(line.lstrip("#"))
            output.append(f"<h{level}>{inline(line[level:].strip())}</h{level}>")
        elif line.startswith("!["):
            flush()
            match = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", line)
            if not match:
                raise ValueError("Malformed audit image")
            caption, path = match.groups()
            image = (SOURCE.parent / path).resolve()
            if not image.is_relative_to(SOURCE.parent):
                raise ValueError("Evidence image must stay within docs")
            mime = "image/png" if image.suffix == ".png" else "image/jpeg"
            data = base64.b64encode(image.read_bytes()).decode("ascii")
            output.append(f'<figure><img src="data:{mime};base64,{data}" '
                          f'alt="{html.escape(caption)}"><figcaption>'
                          f'{inline(caption)}</figcaption></figure>')
        elif not line.strip():
            flush()
        elif line.startswith("- "):
            flush()
            paragraph.append("• " + line[2:])
        else:
            paragraph.append(line)
    flush()
    return "\n".join(output)


STYLE = """
body{margin:0;background:#f5f7fa;color:#1e293b;font:16px/1.65 system-ui,sans-serif}
main{max-width:1150px;margin:40px auto;padding:44px;background:white;border-radius:16px}
h1{font-size:36px;line-height:1.2;color:#12263a}h2{margin-top:44px;font-size:24px}
h3{margin-top:28px}strong{color:#12263a}code{font-size:.87em;background:#edf2f7;padding:2px 4px}
pre{padding:18px;background:#edf2f7;border-radius:8px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px;line-height:1.5}
.table-scroll{overflow-x:auto}th,td{padding:13px;text-align:left;vertical-align:top;border:1px solid #dce3ed}
th{background:#eaf0f7}tr:nth-child(even){background:#fafbfd}
figure{margin:24px 0;border:1px solid #dce3ed;border-radius:8px;padding:14px}
img{display:block;max-width:100%;max-height:900px;object-fit:contain;margin:auto}
figcaption{font-size:14px;color:#526276;margin-top:12px}
@media(max-width:700px){main{margin:0;padding:22px}h1{font-size:28px}table{min-width:800px}}
@media print{body{background:white}main{margin:0;padding:0}h2{break-after:avoid}tr,figure{break-inside:avoid}}
"""

if __name__ == "__main__":
    content = render(SOURCE.read_text(encoding="utf-8"))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>SceneIt customer journey audit</title>'
        f"<style>{STYLE}</style></head><body><main>{content}</main></body></html>",
        encoding="utf-8",
    )
    print(OUTPUT.relative_to(ROOT))