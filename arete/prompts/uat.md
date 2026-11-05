Work for ticket $ref was merged and deployed to `$branch`. A person will now check it there by hand, using the
product the way its users do. Write the steps they follow.

## Ticket $ref: $title

$ticket

## Pull requests

$prs

The diff is the source of truth for what changed. The working directory holds the repo, maybe without this
change; read it only to find where things live for a user: page routes, menu and button names, commands,
endpoints, and the address of the `$branch` environment if the repo documents one.

Write for someone using the product in `$branch`, not someone reading code: what to open, what to click or
type, and what they should see. Cover the main path the ticket asks for first, then each edge or error case
the change handles. Ask for a terminal only when the change has no screen (an API, a job, a CLI); then give
the exact request or command and the response to expect. Put any account, data or setup they need first in
`before`, or leave it empty. One line per step, each with its expected result, at most 10 steps.
