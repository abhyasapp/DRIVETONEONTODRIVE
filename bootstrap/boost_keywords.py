"""
boost_keywords.py
-----------------
Adds targeted keywords to the existing *_keywords.json files.
Reads the current file, merges new keywords, saves back.

Safe to run multiple times (deduplicates).
"""

import json, os

# =====================================================================
# NEW KEYWORDS FROM UNCLASSIFIED SAMPLES
# Structure: { filename: { "chapter_key": { "code title": [new words] } } }
# =====================================================================

ADDITIONS = {
    "l5_keywords.json": {
        "1": {
            "1.1 General":              ["tacheometer","tacheometric","additive constant","multiplying constant","stadia constant","branch of surveying","instrumental observation","horizontal distance","vertical distance"],
            "1.4 Theodolite and Traverse": ["magnetic declination","diurnal variation","declination","isogonic","agonic","magnetic meridian"],
        },
        "2": {
            "2.2 Cement":               ["compressive strength","cube test","size of cube","cement test","rcc work","r.c.c work","general rcc"],
            "2.3 Clay and Clay Products": ["concrete mix","slump test","slump","workability test"],
        },
        "3": {
            "3.1 Mechanics of Materials": ["hooke's law","hooke law","deformation per unit length","type of load","load types","time independent load","static load","dead load","live load"],
        },
        "4": {
            "4.3 Measurement of Discharge": ["orifice","orifices","pitot tube","pitot-tube","venturimeter","venturi meter","measure discharge","measure velocity"],
        },
        "5": {
            "5.4 Shear Strength of Soils": ["toughness test","toughness","plastic limit","shrinkage limit","plasticity","atterberg","plastic limit shrinkage limit","difference between plastic and shrinkage"],
            "5.3 Compaction of Soil":    ["consolidation","drainage path","time required consolidation","rate of consolidation"],
        },
        "6": {
            "6.1 RC Sections in Bending": ["under reinforced section","under-reinforced","load factor","ultimate strength design","combined load factor","rcc design","r.c.c design","design criterion"],
            "6.2 Shear and Bond for RC": ["design criterion rcc wall","rcc wall"],
        },
        "7": {
            "7.1 Foundations":          ["soil investigation","penetrometer","penetration test","raft foundation","raft","combined footing","footing connected by beam","strap footing","soil exploration","soil investigation method"],
        },
        "8": {
            "8.4 Excreta Disposal":     ["bod","bod test","biological oxygen demand","biochemical oxygen demand","undesirable gas","domestic water","trap","water trap","sewer trap"],
        },
        "9": {
            "9.3 Irrigation Canals":    ["water way","length of water way","cross drainage work","meter fall","kennedy relation","kennedy's relation","canal fall"],
        },
        "10": {
            "10.2 Geometric Design":    ["lemniscate","lemniscate curve","maximum polar angle","radial distance","design capacity","super-elevation","superelevation","snow bound","snow area"],
        },
        "11": {
            "11.1 General":             ["unit of measurement","brick work","flat soiling","kilo-liter","kiloliter","cubic meter","reduced level","rl","longitudinal section"],
        },
        "12": {
            "12.1 Organization":        ["conception of idea","construction work idea","project cost","price adjustment","sheep foot rolling","sheepsfoot","sheepfoot"],
            "12.5 Planning and Control": ["maximum project cost","project cost after price adjustment"],
        },
        "13": {
            "13.1 General":             ["icao","i.c.a.o","crosswind","crosswind component","landing area","multiengine","multi-engine","helicopter","ifr","acn","aircraft classification number"],
        },
    },

    "gk_keywords.json": {
        "1": {
            "1.9 REGIONAL ORGS":         ["saarc tb centre","stac","saarc tb","tuberculosis centre","saarc centre"],
            "1.3 PERIODIC PLAN":         ["state-led","planned development","state led planned development","planned project","planned development project"],
            "1.7 CONSTITUTION":          ["constitution of nepal","national assembly","minimum age","age criteria"],
        },
        "2": {
            "2.1 OFFICE MGMT":           ["supervisor","evaluation mark","maximum evaluation","issuing orders","providing guidance","subordinate","subordinates"],
            "2.2 CIVIL SERVICE ACT":     ["constitution of nepal","national assembly","age criteria"],
        },
    },

    "l7_keywords.json": {
        "1": {
            "3.1 CG&MOI":                ["limit strength design","limit state design","fixed support","space structure","space frame"],
            "3.4 DETERMINATE STR":       ["influence line","influence lines","influence diagram"],
        },
        "2": {
            "4.1 INTRO&CLASSIFICATION":  ["positive error","error","measurement error","allowable limit","true value","sensitiveness","bubble tube sensitivity"],
            "4.5 LEVELING":              ["bubble tube","sensitiveness of bubble tube"],
        },
        "3": {
            "5.4 CEMENTING MATERIALS":   ["snowcem","le chatelier","le-chatelier","le chatelier's apparatus","soundness apparatus"],
            "5.7 MISC MATERIALS":        ["special material","high temperature material","refractory","heat resistant","fire resistant material"],
        },
        "4": {
            "6.4 MIXING&CURING":         ["construction joint","construction joints","joint"],
            "6.2 W-C RATIO":             ["ferrocement","ferro cement","ferrocement mix","water cement ratio ferrocement"],
        },
        "5": {
            "7.3 WATER IN SOIL":         ["quick condition","quick sand","quicksand","cohesionless soil","loses cohesion"],
            "7.5 ROCK&EARTHQUAKE":       ["graphical method","earth pressure","culmann","culmann's method","determination of earth pressure"],
            "7.1 SOIL FORMATION":        ["particle of different size","particles of different sizes","good proportion","well graded","gradation"],
        },
        "6": {
            "8.5 PROJECT MGMT":          ["stakeholder","stakeholders","resolution of conflict","balance needs","project stakeholder"],
            "8.1 SCHEDULING&PLANNING":   ["bar chart","horizontal bar","upper portion","milestone chart","milestone"],
        },
        "7": {
            "9.1 TYPES OF ESTIMATES":    ["centre line method","center line method","centre line","estimating building"],
            "9.6 VALUATION":             ["net annual letting value","letting value","yearly repair","gross income","net income"],
        },
        "8": {
            "10.4 DRAFTING TOOLS":       ["pencil","light line","drawing pencil","pencil grade"],
            "10.6 TOPO&SERVICE DWG":     ["technical drawing","manufacturing drawing","civil construction drawing","set of drawings"],
            "10.3 PROJECTION THEORY":    ["angle between horizontal lines","horizontal line angle"],
        },
        "9": {
            "11.4 COST CONCEPTS":        ["additional money","selling one more unit","specified level of output","marginal revenue","non-standard job","customer specification","job costing"],
            "11.3 NPV IRR WORTH METHODS": ["fw method","future worth","future worth method","minimum condition","feasible project"],
        },
        "10": {
            "12.4 PUBLIC PROCUREMENT":   ["public work directive","pwd","public work directives","bidding document","bidding documents","construction material rate"],
            "12.6 BUILDING BYLAWS":      ["construction material rate fixation committee","rate fixation committee"],
        },
    },
}


def merge_keywords(filepath, additions):
    if not os.path.exists(filepath):
        print(f"  SKIP {filepath} (not found)")
        return 0, 0
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    added_count = 0
    total_after = 0

    for ch_key, subs in additions.items():
        if ch_key not in data:
            data[ch_key] = {}
        for full_key, new_words in subs.items():
            existing = data[ch_key].get(full_key, [])
            existing_lower = {w.lower() for w in existing}
            for w in new_words:
                if w.lower() not in existing_lower:
                    existing.append(w)
                    existing_lower.add(w.lower())
                    added_count += 1
            data[ch_key][full_key] = existing
            total_after += len(existing)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return added_count, total_after


def main():
    total_added = 0
    for fname, additions in ADDITIONS.items():
        added, after = merge_keywords(fname, additions)
        print(f"{fname}: +{added} new keywords  ({after} total)")
        total_added += added
    print(f"\nTotal new keywords added: {total_added}")


if __name__ == "__main__":
    main()