# Tally Stock Viewer - Project Solution (Updated)

## Status: OPERATIONAL ✓
The Tally Stock Viewer application is functional, employing a flat-list matching strategy enhanced by a Human-in-the-Loop (HITL) learning architecture.

---

## 1. Core Architecture: The "Flat-List" Pivot
Initially, the project attempted to parse Tally's explicit parent-child hierarchy from Excel exports. This was found to be brittle because Tally's visual formatting (bolding/indentation) is lost during Excel generation. 

**The Solution:**
- **Flat Ingestion:** Load all designs (approx. 3,196) from `main.xls` as a flat list.
- **Relational Mapping:** Connect car models from the dropdown to these designs using a multi-layered matching engine.
- **Live Sync:** Stock quantities are updated every 3 minutes via XML-over-HTTP from Tally, ensuring the "Flat List" always shows real-time availability.

---

## 2. Advanced Matching & Normalization Engine
To solve the "Human Naming Chaos" (e.g., inconsistencies between Tally names and folder names), the system uses a normalization pipeline before attempting any match.

### Normalization Pipeline:
1.  **Standardization:** All text is converted to uppercase and extra whitespace is collapsed.
2.  **Symbol Stripping:** Brittle characters (e.g., `*`, `-`, `.`, `(`, `)`) are removed.
3.  **Synonym Translation (The Business Logic Layer):** Automatically translates industry-specific shorthand:
    - `WA` or `(WA)` → `ARMS`
    - `2PCS` → `2 PCS`
    - `H/R` → `HEADREST`
4.  **Base Extraction:** Removes version markers (like `** V-18 **`) to isolate the core car model name.

---

## 3. Human-in-the-Loop (HITL) Learning System
This system is designed to "learn" the specific business language of the operator. Instead of failing on complex naming variations, it asks the user for a one-time confirmation and remembers it forever.

### The "Ask-and-Remember" Loop:
- **Query:** When a car is selected, the system first checks its persistent database (`mapping_memory.json`).
- **Suggestion:** If no record exists, the system uses fuzzy logic to suggest the most likely folder from the thousands of images in `data/S.S IMAGE`.
- **Validation:** The operator confirms or corrects the suggestion in the UI.
- **Learning:** Upon confirmation, the system creates a permanent link: `Tally_Name -> Image_Folder`.
- **Database Growth:** Over time, the system builds a comprehensive, functional database that handles the inherent naming chaos without requiring code updates.

### Image Handling Improvements
- Individual images can be copied from the UI using a PNG-safe clipboard path.
- Bulk export is supported for the currently visible image set and the training queue.
- The visible scan button was removed from the main UI; image ingestion now points to `data/S.S IMAGE`.

---

## 4. Confidence Level Tiers
Every match is assigned a confidence label to prioritize the operator's attention:

| Status | Condition | Operator Action |
| :--- | :--- | :--- |
| ✅ **Confident** | Exact normalized match or found in `mapping_memory.json`. | No action; displayed automatically. |
| ⚠️ **Possible** | Substring match found or high fuzzy logic score (>75%). | **Manual Confirmation Required** (one-time). |
| ❌ **Missing** | No folder found within the confidence threshold. | **Manual Folder Assignment**. |

---

## 5. Planned: Picture Management & Learning Gallery
Building on the HITL architecture, the system will evolve into a visual inventory manager:
- **Auto-Organization:** Pictures are grouped by car model using the learned mapping database.
- **Progressive Database:** As more images are confirmed, the "Picture-to-Stock" association becomes more accurate.
- **Similarity Scoring:** Future versions will use similarity scores to suggest matches for new, unmapped images.

---

## 6. Historical Strategies & Archive
*Note: These strategies were tested and documented for future reference/recovery if needed.*

### Hierarchical Parser (Failed Strategy)
**Problem:** Attempted to parse parent-child structures based on row positioning and formatting.
**Reason for Failure:** Tally's Excel export is a flattened representation. Heuristics couldn't reliably distinguish groups from items across 3,000+ rows without losing data integrity.

### Direct XML Mapping (Limited Strategy)
**Problem:** Attempted to rely solely on Tally IDs.
**Reason for Failure:** Filesystem folders (images) are created by humans and do not contain ERP IDs. A "human-to-machine" bridge (the HITL system) was required instead.

---

## 7. Technical Implementation Summary
- **Backend:** Flask API handling normalization, Tally XML fetching, and JSON memory management.
- **Frontend:** Streamlit/HTML UI for dropdown selection and HITL confirmation prompts.
- **Storage:** `main.xlsx` (Structure), `item stock list.auto.xlsx` (Live Qty), and `mapping_memory.json` (Learned Logic).
