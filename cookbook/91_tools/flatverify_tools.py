"""
Flatverify Email Verification Tools
===================================

Demonstrates an agent that checks whether email addresses can receive mail
before they are used, one at a time or as a whole list.

Requirements:
- A Flatverify API key from https://app.flatverify.com (API keys page)

Set the following environment variable (or pass the key to FlatverifyTools):

    export FLATVERIFY_API_KEY="fv_..."
"""

from agno.agent import Agent
from agno.models.openai import OpenAIResponses
from agno.tools.flatverify import FlatverifyTools

agent = Agent(
    name="Lead List Checker",
    model=OpenAIResponses(id="gpt-5.5"),
    tools=[FlatverifyTools()],
    instructions=[
        "Use verify_emails for more than one address and verify_email for a single address.",
        "Group the results by status and say which addresses are safe to use.",
        "Treat catch_all and role_account results as risky and say why.",
    ],
    markdown=True,
)


if __name__ == "__main__":
    agent.print_response(
        "Check these leads before I import them: maya.chen@amazon.com, "
        "j.ortiz@salesforce.com, sam.lee@stripe.com, info@shopify.com"
    )
