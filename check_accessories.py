import app as app_module
import xml.etree.ElementTree as ET

xml_req = """<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export Data</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>List of Stock Items with Parent</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="List of Stock Items with Parent" ISMODIFY="No">
                        <TYPE>StockItem</TYPE>
                        <FETCH>NAME, PARENT</FETCH>
                    </COLLECTION>
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>"""

import re

text = app_module._post_tally_with_retry(xml_req)

# Save raw response for inspection
with open("raw_accessories_dump.xml", "w", encoding="utf-8") as f:
    f.write(text)

# Show what's around the reported error location (line 760, col 27) if present
lines = text.splitlines()
if len(lines) >= 760:
    print("--- context around line 760 ---")
    for i in range(max(0, 755), min(len(lines), 765)):
        print(i + 1, repr(lines[i][:120]))
    print("--- end context ---")
    print()

# Strip raw control characters
text = re.sub(u"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)

# Strip invalid numeric XML character references like &#0; through &#8; etc,
# and any reference pointing to a character outside the valid XML range
def _strip_invalid_char_ref(match):
    try:
        code = int(match.group(1))
    except ValueError:
        return ""
    if code in (0x9, 0xA, 0xD) or (0x20 <= code <= 0xD7FF) or (0xE000 <= code <= 0xFFFD):
        return match.group(0)
    return ""

text = re.sub(r"&#(\d+);", _strip_invalid_char_ref, text)
text = re.sub(r"&#x([0-9A-Fa-f]+);", lambda m: _strip_invalid_char_ref_hex(m), text) if False else text

root = ET.fromstring(text)

count = 0
all_parents = set()
for item in root.iter("STOCKITEM"):
    name = item.attrib.get("NAME", "")
    parent_node = item.find("PARENT")
    parent = parent_node.text if parent_node is not None else ""
    all_parents.add(parent.strip())
    if parent and parent.strip().upper() == "ACCESSORIES":
        print(name, "| parent:", parent)
        count += 1

print("Total items with PARENT exactly ACCESSORIES:", count)
print()
print("All distinct PARENT values containing 'ACCESSOR':")
for p in sorted(all_parents):
    if "ACCESSOR" in p.upper():
        print(" -", repr(p))
