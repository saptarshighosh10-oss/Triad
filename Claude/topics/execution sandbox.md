# execution sandbox

Stage 2 runs free-model-**generated** code, so 'never give cheap models something you can't `git reset`' isn't enough — you need a real sandbox (subprocess/container, no host FS, no network, time/memory limits). Never on the host.

Related: [[generate-verify-select]], [[oracle independence]].
