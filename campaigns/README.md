# Campaigns

Each `.yaml` file in this directory is a self-contained outreach campaign.

To add a new one (e.g. VC fundraising later):

1. Copy `internship.yaml` to `your-campaign.yaml`
2. Edit `goal`, `audience`, `sender_bio`, `ask`, `tone`
3. Run: `uv run outreach run --campaign your-campaign ...`

Fields:

| field           | required | what it does                                         |
| --------------- | -------- | ---------------------------------------------------- |
| `name`          | yes      | Must match the filename (without `.yaml`).           |
| `goal`          | yes      | One sentence describing why you're reaching out.     |
| `audience`      | yes      | Who you're targeting. Shapes how the LLM speaks.     |
| `sender_bio`    | yes      | Your background, in the LLM's voice.                 |
| `ask`           | yes      | The specific ask at the end of the message.          |
| `tone`          | yes      | Style guidance: casual, formal, length, etc.         |
| `attach_resume` | no       | Whether to attach the resume PDF. Default: `true`.   |
