"""Non-functional CrowAI model package template."""


def prepare_request(*, question, language, interaction_mode, conversation, attachments, memory_snapshot=None):
    return {
        "request_question": question,
        "query_variations": [],
        "metadata": {"template_only": True},
    }


def finalize_result(*, question, language, interaction_mode, result):
    raise RuntimeError("Implement a reviewed model pipeline before installing this development template.")


def health_check():
    return {"ok": False, "status": "template_only"}
