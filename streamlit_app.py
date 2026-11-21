# streamlit_app.py
# Beauty Contest — guess a number 0-100; closest to 2/3 of the class average wins.
# Commit (hidden guess) -> Reveal (show guess, checked against commit) -> Results.
# Everything is saved inside the app. No setup needed.

import csv
import hashlib
import os
import threading
from datetime import datetime, timezone
from statistics import mean

import pandas as pd
import streamlit as st

APP_NAME = "Beauty Contest"
K_FACTOR = 2 / 3
MIN_N, MAX_N = 0, 100

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
COMMITS_FILE = os.path.join(DATA_DIR, "commits.csv")
REVEALS_FILE = os.path.join(DATA_DIR, "reveals.csv")
COMMIT_COLS = ["timestamp_utc", "uni_id", "commit"]
REVEAL_COLS = ["timestamp_utc", "uni_id", "number", "nonce"]

LOCK = threading.Lock()


# ---------------------------------------------------------------- storage
def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def append_row(path, cols, row):
    with LOCK:
        os.makedirs(DATA_DIR, exist_ok=True)
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            if new:
                w.writeheader()
            w.writerow(row)


def read_rows(path):
    if not os.path.exists(path):
        return []
    with LOCK, open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------- rules
# Same rules as merge_consensus_from_api.py:
#  latest reveal per student -> latest commit at or before it -> hash check -> range check
def check_student(uid, commits, reveals):
    mine_r = sorted([r for r in reveals if r["uni_id"] == uid], key=lambda r: r["timestamp_utc"])
    if not mine_r:
        return None
    rev = mine_r[-1]
    mine_c = sorted([c for c in commits if c["uni_id"] == uid and c["timestamp_utc"] <= rev["timestamp_utc"]],
                    key=lambda c: c["timestamp_utc"])
    try:
        num = int(rev["number"])
    except ValueError:
        num = None
    if not mine_c:
        ok, reason = False, "No commit found before this reveal"
    elif num is None or not (MIN_N <= num <= MAX_N):
        ok, reason = False, "Number out of range"
    elif sha256(f"{uid}|{num}|{rev['nonce']}") == mine_c[-1]["commit"]:
        ok, reason = True, "Verified"
    else:
        ok, reason = False, "Does not match the commit (wrong number or secret)"
    return {"University ID": uid, "Number": num, "Verified": ok, "Reason": reason,
            "Revealed at (UTC)": rev["timestamp_utc"][:19].replace("T", " ")}


def compute_results():
    commits, reveals = read_rows(COMMITS_FILE), read_rows(REVEALS_FILE)
    rows = [check_student(uid, commits, reveals) for uid in sorted({r["uni_id"] for r in reveals})]
    valid = [r for r in rows if r["Verified"]]
    avg = target = None
    if valid:
        avg = mean(r["Number"] for r in valid)
        target = K_FACTOR * avg
        for r in rows:
            r["Distance"] = round(abs(r["Number"] - target), 4) if r["Verified"] else None
        best = min(r["Distance"] for r in valid)
        for r in rows:
            r["Winner"] = "🏆" if r["Verified"] and abs(r["Distance"] - best) < 1e-9 else ""
        rows.sort(key=lambda r: (not r["Verified"], r["Distance"] if r["Distance"] is not None else 1e9))
    return rows, valid, avg, target, len(commits)


# ---------------------------------------------------------------- page
st.set_page_config(page_title=APP_NAME, page_icon="🎯", layout="centered")
st.title(f"🎯 {APP_NAME}")
st.write("Pick a number from **0 to 100**. The winner is whoever is closest to **⅔ of the class average**.")

tab_commit, tab_reveal, tab_results = st.tabs(["Commit", "Reveal", "Results"])

# ---------------- Commit
with tab_commit:
    st.subheader("Commit")
    st.write("Enter your details. Your number stays hidden until you reveal it.")
    with st.form("commit_form", clear_on_submit=True):
        uid = st.text_input("University ID")
        num = st.number_input("Your number (0–100)", min_value=MIN_N, max_value=MAX_N, step=1, value=None,
                              placeholder="Choose a number")
        secret = st.text_input("Secret password (any word you'll remember)", type="password")
        go = st.form_submit_button("Submit commit", type="primary", use_container_width=True)
    if go:
        uid, secret = uid.strip(), secret.strip()
        if not uid or num is None or not secret:
            st.error("Please fill in all three fields.")
        else:
            append_row(COMMITS_FILE, COMMIT_COLS,
                       {"timestamp_utc": now_iso(), "uni_id": uid,
                        "commit": sha256(f"{uid}|{int(num)}|{secret}")})
            st.success(f"✅ Commit saved for **{uid}**. Remember your number and secret password: "
                       "you need both to reveal.")

# ---------------- Reveal
with tab_reveal:
    st.subheader("Reveal")
    st.write("Enter the **same** details you used in Commit.")
    with st.form("reveal_form", clear_on_submit=True):
        r_uid = st.text_input("University ID")
        r_num = st.number_input("Your number (0–100)", min_value=MIN_N, max_value=MAX_N, step=1, value=None,
                                placeholder="Choose a number")
        r_secret = st.text_input("Secret password", type="password")
        r_go = st.form_submit_button("Submit reveal", type="primary", use_container_width=True)
    if r_go:
        r_uid, r_secret = r_uid.strip(), r_secret.strip()
        if not r_uid or r_num is None or not r_secret:
            st.error("Please fill in all three fields.")
        elif not any(c["uni_id"] == r_uid for c in read_rows(COMMITS_FILE)):
            st.error(f"No commit found for **{r_uid}**. Submit a commit first.")
        else:
            append_row(REVEALS_FILE, REVEAL_COLS,
                       {"timestamp_utc": now_iso(), "uni_id": r_uid, "number": int(r_num), "nonce": r_secret})
            res = check_student(r_uid, read_rows(COMMITS_FILE), read_rows(REVEALS_FILE))
            if res["Verified"]:
                st.success(f"✅ Reveal verified. **{r_uid}** chose **{res['Number']}**, "
                           "and it matches your commit.")
            else:
                st.error(f"❌ Reveal does not match your commit. {res['Reason']}. "
                         "Check your number and secret password and try again.")

# ---------------- Results
with tab_results:
    st.subheader("Results")
    if st.button("Show results", type="primary", use_container_width=True) or st.session_state.get("shown"):
        st.session_state["shown"] = True
        rows, valid, avg, target, n_commits = compute_results()
        c1, c2, c3 = st.columns(3)
        c1.metric("Commits", n_commits)
        c2.metric("Verified reveals", len(valid))
        c3.metric("Target (⅔ × average)", f"{target:.2f}" if target is not None else "–")
        if not valid:
            st.info("No verified reveals yet.")
        else:
            st.write(f"Class average: **{avg:.2f}**")
            winners = [r for r in rows if r.get("Winner")]
            st.success("🏆 Winner: " + ", ".join(f"**{w['University ID']}** with **{w['Number']}**"
                                                 for w in winners))
            df = pd.DataFrame(rows)[["Winner", "University ID", "Number", "Distance", "Verified", "Reason",
                                     "Revealed at (UTC)"]]
            st.dataframe(df, use_container_width=True, hide_index=True)
            st.download_button("Download results (CSV)", df.to_csv(index=False).encode("utf-8"),
                               "beauty_contest_results.csv", "text/csv", use_container_width=True)
