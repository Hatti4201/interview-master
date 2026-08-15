"""Small, deployment-safe helpers used by the API runtime."""

import re
import subprocess


DEFAULT_DATABASE_URL = (
    "postgresql://interview_master:local_interview_only@127.0.0.1:54329/interview_master"
)

INTRO_ALIASES = {
    "brief introduction", "brief self introduction", "intro", "intro yourself",
    "introduction", "introduce yourself", "please introduce yourself", "self-intro",
    "self intro", "self introduction", "self-introduction", "tell me about yourself",
    "tell us about yourself", "介绍一下你自己", "介绍一下自己", "简单介绍一下自己",
    "简单自我介绍", "自我介绍",
}

CATEGORY_RULES = [
    ("自我介绍与行为面试", r"tell me about yourself|introduce yourself|self[- ]?intro|brief introduction|\bintro\b|strength|weakness|conflict|behavioral|leadership|teamwork|deadline|feedback|why (?:do you want|did you leave|should we|are you interested)|motivates you"),
    ("安全、认证与权限", r"security|authentication|authorization|oauth|jwt|token|xss|csrf|cors|sql injection|encrypt|decrypt|hashing|password|ssl|tls|access control|spring security|identity provider"),
    ("测试与质量保障", r"\btest(?:ing|ed|s)?\b|unit test|integration test|e2e|end.to.end test|selenium|cypress|playwright|jest|junit|mockito|quality assurance|regression|code coverage|sdet"),
    ("云、DevOps 与容器", r"\baws\b|azure|\bgcp\b|cloud|docker|kubernetes|\bk8s\b|jenkins|ci.?cd|devops|terraform|deployment|deploy|container|lambda|ec2|s3 bucket|cloudwatch|prometheus|grafana|helm|openshift"),
    ("数据库、SQL 与缓存", r"\bsql\b|database|postgres|mysql|oracle|mongodb|dynamodb|cassandra|redis|cache|indexing|stored procedure|normalization|acid|transaction isolation|join\b|query optimization|hibernate|jpa|entity framework"),
    ("React、Angular 与前端", r"react|angular|frontend|front.end|html|css|redux|useeffect|usestate|hook\b|virtual dom|component|next\.?js|webpack|vite|browser|responsive|rxjs|ngrx|dom\b|spa\b|micro.?frontend"),
    ("JavaScript、TypeScript 与 Node.js", r"javascript|typescript|node\.?js|\bnode\b|express\.?js|event loop|promise|async.?await|closure|hoisting|npm\b|commonjs|es6|prototype chain"),
    ("Java、Spring 与 JVM", r"\bjava\b|spring|jvm|garbage collect|hashcode|arraylist|linkedlist|concurrenthashmap|maven|gradle|servlet|bean\b|dependency injection|kotlin"),
    ("Python", r"python|django|flask|fastapi|pytest|decorator|generator|gil\b|list comprehension|tuple\b|pandas|numpy"),
    ("AI、机器学习与数据工程", r"machine learning|artificial intelligence|\bai\b|\bml\b|llm|rag\b|large language model|neural|model training|prompt|vector database|embedding|spark|pyspark|airflow|etl\b|data pipeline|data warehouse|data lake|hadoop|snowflake|databricks"),
    ("系统设计、架构与分布式系统", r"system design|architecture|distributed|scalab|high availability|fault toler|load balanc|design pattern|solid principle|domain.driven|\bddd\b|saga pattern|cqrs|event.driven|eventual consistency|circuit breaker|service discovery|message queue|rabbitmq|\bkafka\b|microservice architecture|monolith|bff layer"),
    ("后端、API 与微服务", r"backend|back.end|rest(?:ful)?|graphql|\bapi\b|microservice|spring mvc|http method|web service|endpoint|grpc|controller|middleware|request|response|server"),
    ("编程题、算法与数据结构", r"algorithm|data structure|leetcode|coding challenge|write (?:a |the )?(?:function|program|code)|implement (?:a |the )?(?:function|method|class)|time complexity|space complexity|big.?o|binary tree|binary search|linked list|\barray\b|palindrome|fibonacci|two sum|sort(?:ing)? algorithm|reverse (?:a )?string|code snippet"),
    ("计算机基础、OOP 与并发", r"\boop\b|object.oriented|inheritance|polymorphism|encapsulation|abstraction|interface|abstract class|thread|concurren|deadlock|synchroniz|mutex|semaphore|process vs|memory leak|stack vs heap|compiler|runtime|operating system|network protocol|tcp|udp"),
    ("项目经历与工程实践", r"recent project|your project|project experience|walk (?:me|us) through|day.to.day|role and responsibilit|your resume|production issue|team size|what have you (?:done|built|worked)|your experience|problems did you encounter|challenges did you face|code review|agile|scrum|git\b|pull request"),
]


def run_psql(database_url: str, *args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["psql", database_url, "-v", "ON_ERROR_STOP=1", *args],
        input=input_text,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def language_of(text: str) -> str:
    has_zh = bool(re.search(r"[\u3400-\u9fff]", text))
    has_en = bool(re.search(r"[A-Za-z]", text))
    if has_zh and has_en:
        return "mixed"
    if has_zh:
        return "zh"
    if has_en:
        return "en"
    return "unknown"


def dedupe_key(text: str) -> str:
    key = text.casefold().translate(str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-"}))
    key = re.sub(r"^(?:q(?:uestion)?\s*)?\d+\s*[.):\-]\s*", "", key)
    key = re.sub(r"\s+", " ", key).strip().rstrip(" ?.!;:")
    return "tell me about yourself" if key in INTRO_ALIASES else key


def classify(key: str) -> str:
    for category, pattern in CATEGORY_RULES:
        if re.search(pattern, key):
            return category
    return "综合与其他问题"


if __name__ == "__main__":
    assert dedupe_key("Q1. Introduce yourself?") == "tell me about yourself"
    assert language_of("解释 Spring") == "mixed"
    assert classify("how does spring dependency injection work") == "Java、Spring 与 JVM"
    print("runtime support self-test passed")
