# LPU × Amulet: agent review challenge

Review this supplied version of Amulet's BEEP workflow-review agent. Find real bugs, recommend useful improvements and show how you reached your conclusions. You may include fixes or feature work, but you do not need to rebuild the application.

**Review weekend:** 26–27 September 2026. Start when you receive the pack.
**Submission deadline:** **11:59 pm IST on Sunday 27 September 2026** (UTC+05:30).
**Submission route:** your own **private GitHub repository**, shared with **`nayef-sudo`**. Email the repository link and full final commit ID to **nayef@amulet.ai** by the deadline.

## Start here

1. Read this brief and `SCORING.md`.
2. Copy `REVIEW-TEMPLATE.md` to `REVIEW.md` for your response.
3. Read `submission/README.md` for the source map and supported setup/checks. Choose an area you can investigate well; an exhaustive audit is not expected.
4. Keep `BASELINE.json` unchanged. It identifies the common source and instructions you received.
5. Create your private GitHub repository and send the collaborator invitation early. Finish the review, commit it, then email the exact final commit ID.

## What we are building

BEEP is intended to observe a workflow-review session with the participant's consent, listen to the discussion, retain relevant screen evidence, and produce a report that a person can check and correct. The supplied project includes the real Python backend, React frontend and tests from a cleaned, frozen development snapshot. We have not inserted exercise bugs or repaired application behaviour for this challenge.

The code is under development. The live voice, screen and recording service is **not qualified for production** and is not your required test environment. A documented design preview is simulated, not a functioning AI session. No provider credentials, real client sessions, production access or hosted agent are supplied. Restating these disclosed limits is not a bug finding; a distinct defect in a supplied code path can still qualify if you establish it.

## Your task

Submit as many **distinct, supported contributions** as you can investigate properly:

- **Bugs:** what goes wrong, the conditions that make it reachable, expected versus actual behaviour, impact, likely cause and how you would check a fix.
- **Improvements:** a specific need in this application, a feasible change, its value and trade-offs, and measurable acceptance checks. A well-supported proposal can qualify without implementation.
- **A short summary:** what you would fix first, what you would improve next, what you inspected and what you could not verify.

Every distinct accepted contribution counts. There is no finding-count cap or maximum score. Bug points and improvement points are reported separately. The tiers are **2 points for a basic bug, 3 for a big bug, 10 for a decent improvement and 30 for a great improvement**. Read `SCORING.md` for the evidence thresholds and counting rules. Padding, duplicate symptoms or an impressive-sounding claim do not earn extra credit.

You are not expected to work continuously throughout the weekend. Record your approximate active time honestly. There is no speed bonus. If setup or access fails, email nayef@amulet.ai promptly with the relevant error and continue source review where possible. Do not spend the whole exercise fighting infrastructure. A reported incident will be reviewed; it does not automatically extend the deadline or authorise another submission route.

## Evidence and permitted tools

Separate **observed**, **source-supported but not run**, and **unverified** claims. A complete static code trace can earn full credit when it establishes the cause, reachable conditions and consequence. Do not label a hypothesis as a reproduced bug. Use precise file/line references or UI steps, and include small relevant excerpts rather than unrelated logs.

The supported setup and checks are in `submission/README.md`. The recorded runtime is Python 3.12 and Node.js 22.23.1 on macOS/Linux. Dependency installation needs internet access; the selected review checks do not require paid model services, a database, microphone or screen permissions. Native Windows setup has not been verified. Static source review remains valid if you cannot run the checked environment. Do not run the full retained integration suite or start live services as a shortcut; those paths are outside the documented offline setup.

AI tools and public documentation are allowed. Check their output, understand your findings and briefly declare the help used. We do not need private chat histories. You may use the supplied source and fictional inputs with your chosen coding assistant for this exercise, but do not publish the code, reports or shared-chat links. No paid subscription is required or rewarded.

Use fictional inputs only. Do not test Amulet's live websites, accounts, provider endpoints or other students' repositories. Security findings must be demonstrated locally and reported privately. Never include credentials, personal data, real recordings or unsafe exploit links in your response. Treat code as untrusted and read commands before running them.

Work individually. Do not exchange findings, reports or jointly generated answers with other candidates during the window. Permitted AI/documentation assistance is not permission to share answers with peers.

## Optional implementation evidence

A small patch and regression test under `evidence/` are enough to demonstrate a change. If you prefer a pull request, keep it in **your own private repository**, invite `nayef-sudo`, and include the exact PR head commit in your report. No public fork, public PR or contribution to a shared Amulet repository is needed or permitted for this exercise.

Keep the supplied baseline separate from your changes and describe which files you changed. Do not edit `BASELINE.json` to describe your implementation. A PR title, code volume or screenshots alone do not qualify for 30 points. The same underlying work is credited once, at its highest supported tier, not once as a bug and again as an improvement.

## Submit through private GitHub

1. Create a **private** repository named `lpu-agent-review-<your-registration-number>`.
2. Put `REVIEW.md` and the unchanged supplied `BASELINE.json` at its root. Put optional small evidence, tests or patches under `evidence/`. You do not need to re-upload the whole application unless it is needed for your private implementation/PR.
3. Invite **`nayef-sudo`** as a collaborator through the repository's access settings. Send the invitation early. Keep the repository private.
4. Commit your finished work. Copy the **full final commit ID**, not a shortened hash or a branch name. For a separate implementation branch, record its full commit ID in `REVIEW.md` too.
5. Before **11:59 pm IST on Sunday 27 September 2026**, email **nayef@amulet.ai** with your **name, registration number, repository URL and full final review commit ID**. Suggested subject: `LPU agent review — <registration number>`. Send both the email and the collaborator invitation by the deadline.

We assess the specified commit, not later changes. A correctly sent, timely invitation is not late just because we accept it later. An invitation alone is not the final submission.

GitHub is a basic requirement for this placement exercise. You may consult its documentation, but are expected to create the private repository, commit your work and arrange access yourself. **We do not accept ZIP files, emailed report attachments, shared-drive links or public repositories as alternative submissions.** The ZIP containing this brief is our source distribution, not a student submission option.

Before emailing, check:

- [ ] Repository is private and `nayef-sudo` has been invited.
- [ ] Root contains completed `REVIEW.md` and unchanged `BASELINE.json`.
- [ ] Findings refer to this snapshot; observations and untested claims are distinguished.
- [ ] Optional evidence/implementation references are present and tied to exact commits.
- [ ] No secrets, `node_modules`, `.venv`, copied chat histories or unrelated files are included.
- [ ] Email contains name, registration number, repository URL and full final commit ID.

Your submission remains your work. The exercise does not transfer ownership or guarantee a placement. Any commercial reuse beyond assessment requires a separate agreement. We may ask shortlisted students to explain a finding or walk through a small changed example.
