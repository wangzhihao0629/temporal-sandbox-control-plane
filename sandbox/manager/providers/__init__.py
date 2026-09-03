"""Pool providers: how the manager launches and terminates machines.

What: one Protocol, one implementation per compute backend.
Why: the manager's lifecycle logic must not know whether a VM is an Apple
container, an EC2 instance, or a hosted sandbox. Swapping the provider is the
whole migration story.
Production: an EC2 provider over the ASG and EC2 APIs.
"""
