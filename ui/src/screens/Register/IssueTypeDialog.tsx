import type { IssueTypeQuestion } from '../../api/types';

interface IssueTypeDialogProps {
  question: IssueTypeQuestion;
  onAnswer: (id: string) => void;
  onBack: () => void;
}

/**
 * Asked before the register exists, never after.
 *
 * A partial issue is normal in construction: the consultant reissues the
 * twelve sheets that changed, not all three hundred. Guessing wrong here and
 * reporting "288 drawings removed" is what makes a user close the tool and
 * never open it again, so the question is asked with both counts visible.
 */
export function IssueTypeDialog({ question, onAnswer, onBack }: IssueTypeDialogProps) {
  return (
    <div className="issue-type">
      <div className="issue-type__panel">
        <button type="button" className="register__back" onClick={onBack}>
          ← Setup
        </button>

        <h1 className="issue-type__title">Is this a partial issue?</h1>
        <p className="issue-type__question">{question.question}</p>

        <div className="issue-type__counts tabular">
          <div>
            <span className="issue-type__count">{question.old_count}</span>
            <span className="micro">in the previous issue</span>
          </div>
          <span className="issue-type__seam" aria-hidden="true" />
          <div>
            <span className="issue-type__count">{question.new_count}</span>
            <span className="micro">in the current issue</span>
          </div>
        </div>

        <ul className="issue-type__options">
          {question.options.map((option) => (
            <li key={option.id}>
              <button
                type="button"
                className="issue-type__option"
                onClick={() => onAnswer(option.id)}
              >
                <span className="issue-type__option-label">{option.label}</span>
                <span className="issue-type__option-note">{option.description}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
