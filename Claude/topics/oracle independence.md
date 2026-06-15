# oracle independence

generate-verify-select only works if the verifier isn't captured by the generator. If the free models write both the code and its tests, they'll write tests that pass their own wrong code. The oracle must be independent: user tests, a separate test-author step, or ground-truth execution.

Related: [[generate-verify-select]], [[execution sandbox]].
