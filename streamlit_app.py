# streamlit_app.py
# Beauty Contest — commit / reveal / results on an append-only shared ledger
# (Google Sheets + Apps Script web app). Based on commit_client.py,
# reveal_client.py and merge_consensus_from_api.py.

import csv
import hashlib
import io
from datetime import datetime, timezone
from statistics import mean

import pandas as pd
import requests
import streamlit as st

# ----------------------------------------------------------------------------
# Configuration
# Set this in Streamlit Cloud → App settings → Secrets (or .streamlit/secrets.toml):
#   API_URL = "https://script.google.com/macros/s/XXXX/exec"
# ----------------------------------------------------------------------------
APP_NAME = "Beauty Contest"

API_DEFAULT = "https://script.google.com/macros/s/REPLACE_WITH_YOUR_/exec"

K_FACTOR = 2 / 3
ALLOWED_MIN, ALLOWED_MAX = 0, 100
TS_MIN = datetime.min.replace(tzinfo=timezone.utc)  # aware fallback for bad timestamps


def get_secret(key, default):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


API_URL = get_secret("API_URL", API_DEFAULT)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def parse_ts(ts: str):
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def ledger_url_problem(api: str):
    """Return an error message if the ledger URL is missing or still the placeholder."""
    if not api or "REPLACE_WITH_YOUR_" in api:
        return "No ledger API URL set yet. Paste the /exec link from your instructor in the sidebar."
    if not api.startswith("https://") or not api.rstrip("/").endswith("/exec"):
        return "The ledger API URL should start with https:// and end with /exec."
    return None


def short_reply(text: str) -> str:
    """Keep server replies readable: hide HTML error pages, trim long text."""
    t = (text or "").strip()
    if t.lower().startswith("<!doctype") or t.lower().startswith("<html"):
        return "(the server returned a web page instead of a ledger reply)"
    return t if len(t) <= 300 else t[:300] + "…"


def post_to_ledger(api: str, payload: dict):
    """Returns (ok, message)."""
    try:
        r = requests.post(api, json=payload, timeout=15)
    except Exception as e:
        return False, f"Could not reach the ledger: {e}"
    if 200 <= r.status_code < 300:
        return True, short_reply(r.text)
    hint = " Check the ledger API URL." if r.status_code in (401, 403, 404) else ""
    return False, f"HTTP {r.status_code} {short_reply(r.text)}{hint}"


def get_csv(url: str):
    r = requests.get(url, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} from the ledger. Check the ledger API URL.")
    if r.text.lstrip().lower().startswith(("<!doctype", "<html")):
        raise RuntimeError("The ledger returned a web page instead of CSV. Check the ledger API URL.")
    return list(csv.DictReader(io.StringIO(r.text)))


def to_csv_bytes(fieldnames, rows) -> bytes:
    buf = io.StringIO()
    wr = csv.DictWriter(buf, fieldnames=fieldnames)
    wr.writeheader()
    for r in rows:
        wr.writerow({k: r.get(k, "") for k in fieldnames})
    return buf.getvalue().encode("utf-8")


def valid_inputs(uni_id, number_raw, nonce):
    if not uni_id:
        return None, "University ID cannot be empty."
    try:
        number = int(str(number_raw).strip())
    except ValueError:
        return None, "Number must be an integer."
    if not (ALLOWED_MIN <= number <= ALLOWED_MAX):
        return None, "Number must be between 0 and 100."
    if not nonce:
        return None, "Nonce cannot be empty."
    return number, None


# ----------------------------------------------------------------------------
# Consensus (same deterministic rules as merge_consensus_from_api.py)
# ----------------------------------------------------------------------------
def run_consensus(commits_raw, reveals_raw):
    commits = {}
    for r in commits_raw:
        uid = (r.get("uni_id") or "").strip()
        ch = (r.get("commit") or "").strip()
        ts = (r.get("timestamp_utc") or "").strip()
        if not uid or not ch:
            continue
        commits.setdefault(uid, []).append(
            {"timestamp_utc": ts, "ts_parsed": parse_ts(ts), "uni_id": uid, "commit": ch}
        )

    reveals = {}
    for r in reveals_raw:
        uid = (r.get("uni_id") or "").strip()
        num = (r.get("number") or "").strip()
        nonce = (r.get("nonce") or "").strip()
        ts = (r.get("timestamp_utc") or "").strip()
        if not uid or not num or not nonce:
            continue
        reveals.setdefault(uid, []).append(
            {"timestamp_utc": ts, "ts_parsed": parse_ts(ts), "uni_id": uid,
             "number_raw": num, "nonce": nonce}
        )

    verified_rows, matched_commits = [], {}

    for uid, rlist in reveals.items():
        # 1) latest reveal
        latest_rev = sorted(rlist, key=lambda x: x["ts_parsed"] or TS_MIN)[-1]
        ts_rev = latest_rev["ts_parsed"] or TS_MIN

        try:
            num = int(latest_rev["number_raw"])
        except Exception:
            num = None

        # 2) latest commit at or before that reveal
        cands = [c for c in commits.get(uid, []) if (c["ts_parsed"] or TS_MIN) <= ts_rev]
        cands.sort(key=lambda x: x["ts_parsed"] or TS_MIN)
        chosen = cands[-1] if cands else None
        if chosen:
            matched_commits[uid] = chosen

        # 3) checks
        reason, verified = "ok", False
        if chosen is None:
            reason = "no_commit_before_reveal"
        elif num is None:
            reason = "number_not_int"
        elif not (ALLOWED_MIN <= num <= ALLOWED_MAX):
            reason = "out_of_range"
        else:
            verified = sha256(f"{uid}|{num}|{latest_rev['nonce']}") == chosen["commit"]
            if not verified:
                reason = "hash_mismatch"

        verified_rows.append({
            "uni_id": uid,
            "timestamp_utc": latest_rev["timestamp_utc"],
            "number": "" if num is None else str(num),
            "nonce": latest_rev["nonce"],
            "commit": chosen["commit"] if chosen else "",
            "preimage_hash": "" if num is None else sha256(f"{uid}|{num}|{latest_rev['nonce']}"),
            "verified": "True" if verified else "False",
            "reason": reason,
        })

    valid = [(r["uni_id"], int(r["number"])) for r in verified_rows
             if r["verified"] == "True" and r["number"] != ""]
    if valid:
        avg_val = mean(n for _, n in valid)
        target = K_FACTOR * avg_val
        distances = [(uid, n, abs(n - target)) for uid, n in valid]
        min_dist = min(d for _, _, d in distances)
        winners = [(uid, n, d) for uid, n, d in distances if abs(d - min_dist) < 1e-12]
    else:
        avg_val = target = min_dist = None
        winners = []

    for r in verified_rows:
        if r["verified"] == "True" and target is not None:
            r["distance"] = f"{abs(int(r['number']) - target):.6f}"
        else:
            r["distance"] = ""

    leaderboard = sorted(
        verified_rows,
        key=lambda r: (r["verified"] != "True",
                       float(r["distance"]) if r["distance"] else float("inf"),
                       r["uni_id"]),
    )

    # Output files (same names/columns as the merge script)
    files = {}
    files["merged_commits.csv"] = to_csv_bytes(
        ["timestamp_utc", "uni_id", "commit"],
        sorted(matched_commits.values(), key=lambda r: (r["uni_id"], r["timestamp_utc"])),
    )
    files["merged_reveals.csv"] = to_csv_bytes(
        ["timestamp_utc", "uni_id", "number", "nonce", "preimage_hash", "verified"],
        sorted([r for r in verified_rows if r["verified"] == "True"],
               key=lambda r: (r["uni_id"], r["timestamp_utc"])),
    )
    lb_cols = ["uni_id", "number", "verified", "reason", "distance", "commit", "nonce", "timestamp_utc"]
    files["class_leaderboard.csv"] = to_csv_bytes(lb_cols, leaderboard)

    lines = ["=== CLASS RESULTS (Latest-Attempt Consensus) ===", f"k_factor = {K_FACTOR}"]
    if valid:
        lines += [
            f"participants_counted = {len(valid)}",
            f"average = {avg_val:.6f}",
            f"target = {target:.6f}",
            f"min_distance = {min_dist:.6f}",
            "winners:",
        ] + [f"  - {uid} with {n} (distance {d:.6f})" for uid, n, d in winners]
    else:
        lines.append("No valid verified reveals. Cannot compute winners.")
    files["class_results.txt"] = ("\n".join(lines) + "\n").encode("utf-8")

    return {
        "valid": valid, "avg": avg_val, "target": target, "min_dist": min_dist,
        "winners": winners, "leaderboard": leaderboard, "lb_cols": lb_cols, "files": files,
    }


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------
st.set_page_config(page_title=APP_NAME, page_icon="🎯", layout="centered")

with st.sidebar:
    st.header(APP_NAME)
    st.caption("Guess a number from 0 to 100. Closest to ⅔ of the class average wins.")
    api = st.text_input("Ledger API URL (ends with /exec)", value=API_URL).strip()
    st.divider()
    st.caption("Public ledger: append `?table=commits` or `?table=reveals` to the API URL.")

st.title(f"🎯 {APP_NAME}")

url_problem = ledger_url_problem(api)
if url_problem:
    st.warning(url_problem)

tab_commit, tab_reveal, tab_results = st.tabs(["Commit", "Reveal", "Results"])

# ---------------- Commit ----------------
with tab_commit:
    st.subheader("Commit")
    st.write(
        "Choose your number and a secret nonce. Only the hash "
        "`sha256(uni_id|number|nonce)` is sent to the ledger, so your guess stays hidden."
    )

    with st.form("commit_form"):
        c_uid = st.text_input("Your University ID")
        c_num = st.number_input("Choose your number (0..100)", min_value=0, max_value=100, step=1, value=0)
        c_nonce = st.text_input("Choose a secret nonce (keep it safe)", type="password")
        c_submit = st.form_submit_button("Submit commit", type="primary")

    if c_submit:
        uid, nonce = c_uid.strip(), c_nonce.strip()
        number, err = valid_inputs(uid, c_num, nonce)
        if err:
            st.error(err)
        elif url_problem:
            st.error(f"Commit not sent. {url_problem}")
        else:
            preimage = f"{uid}|{number}|{nonce}"
            commit_hash = sha256(preimage)
            ok, msg = post_to_ledger(api, {"kind": "commit", "uni_id": uid, "commit": commit_hash})
            st.session_state["last_commit"] = {
                "preimage": preimage, "hash": commit_hash, "ok": ok, "msg": msg,
            }

    lc = st.session_state.get("last_commit")
    if lc:
        if lc["ok"]:
            st.success(f"Commit recorded. Server: {lc['msg']}")
        else:
            st.error(f"Commit NOT recorded. {lc['msg']}")
        st.markdown("**Your commit hash**")
        st.code(lc["hash"], language=None)
        st.markdown("**Write this down — you must re-enter it at reveal:**")
        st.code(f"PREIMAGE: {lc['preimage']}", language=None)
        st.download_button(
            "Download my preimage",
            data=f"PREIMAGE: {lc['preimage']}\nCOMMIT: {lc['hash']}\n",
            file_name="my_beauty_contest_preimage.txt",
        )
        st.caption("You can commit again. Your latest commit before your reveal counts.")

# ---------------- Reveal ----------------
with tab_reveal:
    st.subheader("Reveal")
    st.write("Reveal when your instructor says so, re-entering **exactly** what you committed. Anyone can then recompute your hash and verify it.")

    with st.form("reveal_form"):
        r_uid = st.text_input("Your University ID")
        r_num = st.number_input("Your number (0..100)", min_value=0, max_value=100, step=1, value=0)
        r_nonce = st.text_input("Your secret nonce", type="password")
        r_submit = st.form_submit_button("Submit reveal", type="primary")

    if r_submit:
        uid, nonce = r_uid.strip(), r_nonce.strip()
        number, err = valid_inputs(uid, r_num, nonce)
        if err:
            st.error(err)
        elif url_problem:
            st.error(f"Reveal not sent. {url_problem}")
        else:
            ok, msg = post_to_ledger(
                api, {"kind": "reveal", "uni_id": uid, "number": number, "nonce": nonce}
            )
            if ok:
                st.success(f"Reveal recorded. Server: {msg}")
            else:
                st.error(f"Reveal NOT recorded. {msg}")
            st.markdown("**Hash of what you revealed** (should match your commit):")
            st.code(sha256(f"{uid}|{number}|{nonce}"), language=None)
            st.caption("You can reveal again. The latest reveal is matched to your latest prior commit.")

# ---------------- Results ----------------
with tab_results:
    st.subheader("Results")
    st.write(
        "Downloads the public ledger and applies the same deterministic rules for everyone: "
        "latest reveal → latest commit at or before it → hash check → range check. "
        "Target = ⅔ × average of verified numbers."
    )

    if st.button("Compute results from ledger", type="primary"):
        if url_problem:
            st.error(url_problem)
            st.stop()
        try:
            with st.spinner("Downloading commits and reveals…"):
                commits_raw = get_csv(f"{api}?table=commits")
                reveals_raw = get_csv(f"{api}?table=reveals")
            st.session_state["results"] = run_consensus(commits_raw, reveals_raw)
            st.session_state["raw"] = (commits_raw, reveals_raw)
        except Exception as e:
            st.error(f"Could not load the ledger: {e}")

    res = st.session_state.get("results")
    if res:
        if res["valid"]:
            c1, c2, c3 = st.columns(3)
            c1.metric("Participants counted", len(res["valid"]))
            c2.metric("Class average", f"{res['avg']:.4f}")
            c3.metric("Target (⅔ × avg)", f"{res['target']:.4f}")

            st.markdown("### 🏆 Winner(s)")
            for uid, n, d in res["winners"]:
                st.success(f"**{uid}** with **{n}** (distance {d:.6f})")

            st.markdown("### Distribution of verified guesses")
            nums = pd.Series([n for _, n in res["valid"]], name="number")
            dist = nums.value_counts().reindex(range(ALLOWED_MIN, ALLOWED_MAX + 1), fill_value=0)
            st.bar_chart(dist, x_label="Number guessed", y_label="Students")
        else:
            st.info("No valid verified reveals. Cannot compute winners.")

        st.markdown("### Leaderboard")
        lb_df = pd.DataFrame(res["leaderboard"], columns=res["lb_cols"])
        st.dataframe(lb_df, use_container_width=True, hide_index=True)

        st.markdown("### Download outputs")
        cols = st.columns(2)
        for i, (name, data) in enumerate(res["files"].items()):
            cols[i % 2].download_button(
                name, data=data, file_name=name,
                mime="text/plain" if name.endswith(".txt") else "text/csv",
                use_container_width=True,
            )

        commits_raw, reveals_raw = st.session_state.get("raw", ([], []))
        with st.expander(f"Raw ledger — commits ({len(commits_raw)} rows)"):
            st.dataframe(pd.DataFrame(commits_raw), use_container_width=True, hide_index=True)
        with st.expander(f"Raw ledger — reveals ({len(reveals_raw)} rows)"):
            st.dataframe(pd.DataFrame(reveals_raw), use_container_width=True, hide_index=True)
