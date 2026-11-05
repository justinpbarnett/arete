You maintain the expertise file for the agents that work in $repo: what they should know before they plan or
build anything here. A run for $ref ($title) just finished.

## What happened

$story

## Current expertise file

$expertise

The working directory holds the code as this run left it. Rewrite the expertise file in full:

1. Check each lesson in the current file against the code with Read, Glob and Grep. Drop a lesson the code now
   contradicts, such as a file, command, script or convention that is gone or changed. Fix a lesson that is
   only partly wrong.
2. Add at most three lessons from what happened above that would have made this run faster or avoided a
   failure, and that will apply to future, different tickets in this repo. Check each against the code too.
3. Merge lessons that say the same thing. Drop anything specific to one ticket, and generic advice any
   engineer knows.

Each lesson is one line that names the file, command or convention it is about. Start the file with
`# Expertise`, group the lessons under short topic headings, and keep the whole file within $max_lines lines;
when it would run over, drop the lessons least likely to matter on the next ticket. Adding nothing is fine and
common.

Return the whole new file in `expertise`, each new lesson in `added`, and each dropped lesson in `removed`
with the reason the code gave you.
