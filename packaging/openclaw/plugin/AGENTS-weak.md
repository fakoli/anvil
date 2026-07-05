# Working with anvil — step by step

This project is tracked by **anvil**. Follow these steps exactly, ONE at a time,
and check the result of each before moving to the next. Do not improvise the order.

## The loop

1. **See what to do next.** Run: `anvil next`
   - It prints ONE task id (e.g. `T003`) and its title. If it says there are no
     ready tasks, stop — there is nothing for you to do.
2. **Claim that task BEFORE you touch any file.** Run: `anvil claim <task-id>`
   - You now own the task. If you edit a file without claiming first, the guard
     warns you and your work may not count.
3. **Read the task.** Run: `anvil show <task-id>`
   - Read its **Acceptance criteria** (what "done" means) and its **Verification**
     commands (how you prove it). You will need both.
4. **Do the work.** Make the SMALLEST change that meets the acceptance criteria.
   Only edit files related to this task.
5. **Prove it works.** Run every command listed under **Verification** (usually
   the tests). They must all pass. If one fails, your change is wrong — fix it and
   run again. Do not continue with a failing command.
6. **Submit your evidence.** Run: `anvil submit <task-id>`
   - This records that you finished and captures your verification output. If you
     skip this, anvil will BLOCK you from ending your turn.
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
| finish a task | `anvil submit <task-id>` |
