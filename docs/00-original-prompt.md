# Original project prompt

The verbatim request that kicked off this project:

> I want to build a poc voice agent project with Deepgram (you should have the API key). I want to build a voice project with a front end that is for a repair agent.
>
> I want to simulate a user conversation with an AI agent that is an expert in repairing electronic products. it can give basic advice by asking the user questions to understand their problems and then providing safe solutions.
>
> In order to do this we will need:
> - A frontend that caputres user's voice and responds with an AI voice
> - Simulated product instruction manuals that the agent uses as context. These manuals contain information on which actions the agent can suggest to users and which are unsafe and require a specialist to visit them.
> - Safety is paramount, the agent has failed if it recommends a customer doing something unsafe or forbidden by the manual
> - The agent can converse with the user and if the issue needs a specialist, they will book a human engineer to visit them at a future date. They can simulate this, we don't have to build an email or calendar system, just agree verbally with the user about availability and say the engineer is all booked
> - This project should be structured in a way to teach a human engineer about how voice agents with deepgram and STT, LLM, TTS work on a given task. What are the key things to measure such a system and what considerations need to be taken account of to do this well in production
>
> Work with me to tighen things up and make this an excellent and clear demo
