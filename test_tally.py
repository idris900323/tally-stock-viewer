import requests
import xml.etree.ElementTree as ET
import re

TALLY_URL = "http://localhost:9000"

xml_request = """
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Data</TYPE>
        <ID>Stock Summary</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            </STATICVARIABLES>
        </DESC>
    </BODY>
</ENVELOPE>
"""

response = requests.post(TALLY_URL, data=xml_request)
root = ET.fromstring(response.text)

available_items = []

names = root.findall(".//DSPACCNAME")
stocks = root.findall(".//DSPSTKINFO")

for name_node, stock_node in zip(names, stocks):
    name = name_node.find("DSPDISPNAME")
    qty_node = stock_node.find(".//DSPCLQTY")

    item_name = name.text if name is not None else "UNKNOWN"
    qty_text = qty_node.text if (qty_node is not None and qty_node.text) else "0"


    # extract number from "18 NOS"
    match = re.search(r"-?\d+", qty_text)
    qty = int(match.group()) if match else 0

    if qty > 0:
        available_items.append((item_name, qty))

print("TOTAL ITEMS WITH STOCK > 0:", len(available_items))
print("\nFIRST 10 AVAILABLE ITEMS:\n")

for item in available_items[:10]:
    print(item[0], "→", item[1])
