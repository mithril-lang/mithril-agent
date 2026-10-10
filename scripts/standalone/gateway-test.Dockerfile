ARG VERIFIED_IMAGE
FROM ${VERIFIED_IMAGE}
USER root
WORKDIR /verify
COPY . /verify
RUN /opt/hermes/.venv/bin/python -m pm.build_env --source /verify --out /verify/.venv --group dev --group test --extra messaging --sealed
RUN chown -R hermes:hermes /verify && mkdir -p /test-home && chown hermes:hermes /test-home
USER hermes
ENV HOME=/test-home HERMES_HOME=/test-home/hermes HERMES_PYTHON=/verify/.venv/bin/python CI=1
ENTRYPOINT ["bash", "/verify/scripts/run_tests.sh"]
CMD ["tests/gateway/", "tests/hermes_cli/test_container_boot.py", "tests/hermes_cli/test_gateway_external_supervisor.py", "-j", "2"]
