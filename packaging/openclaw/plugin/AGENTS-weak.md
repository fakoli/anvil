# Working with anvil — step by step

This project is tracked by **anvil**. Follow these steps exactly, ONE at a time,
and check the result of each before moving to the next. Do not improvise the order.

## The loop

1. **See what to do next.** Run: `anvil next`
   - It prints ONE task id (e.g. `T003`) and its title. If it says there are no
     ready tasks, stop — there is nothing for you to do.
2. **Claim that task BEFORE you touch any file.** Run: `anvil claim <task-id>`
   - You now own the task. If you edit a file without claiming first, your work is
     NOT tracked to the task — and you will get NO on-screen warning about it, so
     always claim first out of habit.
3. **Read the task.** Run: `anvil show <task-id>`
   - Read its **Acceptance criteria** (what "done" means) and its **Verification**
     commands (how you prove it). You will need both.
4. **Do the work.** Make the SMALLEST change that meets the acceptance criteria.
   Only edit files related to this task.
5. **Prove it works.** Run every command listed under **Verification** (usually
   the tests). They must all pass. If one fails, your change is wrong — fix it and
   run again. Do not continue with a failing command.
6. **Submit your evidence.** You MUST pass the verification command(s) you ran and
   every file you changed — both flags are required:

       anvil submit <task-id> --commands "<verification command>" --files-changed <file>

   - Example: `anvil submit T003 --commands "pytest -q" --files-changed src/foo.py`
   - Use the SAME command(s) from the task's **Verification**. Repeat `--commands`
     once per command and `--files-changed` once per file.
   - Bare `anvil submit <task-id>` (no flags) fails — it will not submit anything.
   - If you skip this step, anvil will BLOCK you from ending your turn.
7. **Done.** Go back to step 1 for the next task.

## Rules — do not break these

- **Claim before you edit.** No file edits without an active claim (step 2).
- **Never end your turn with a claimed task that has no submitted evidence.**
  Always do step 6 first.
- **One task at a time.** Finish and submit the current task before claiming another.
- **A failing verification command means the task is NOT done.** Fix and re-run;
  never submit failing work.
- **Lost? Run `anvil status`.** It shows what you have claimed and what is left.

## Quick reference

| I want to… | Run |
|---|---|
| find the next task | `anvil next` |
| take a task | `anvil claim <task-id>` |
| read a task's requirements | `anvil show <task-id>` |
| see my current state | `anvil status` |
| finish a task | `anvil submit <task-id> --commands "<cmd>" --files-changed <file>` |
