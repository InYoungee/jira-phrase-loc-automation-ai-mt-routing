"""
Main Entry Point for the Jira-Phrase Pipeline.
Executes the resilient batch processing runner.
"""

from batch_runner import run_batch, parse_args


def main():
    print("🚀 Launching Jira-Phrase Localization Automation...")
    args = parse_args()
    run_batch(reprocess_issues=args.reprocess)

if __name__ == "__main__":
    main()