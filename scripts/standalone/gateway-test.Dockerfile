ARG VERIFIED_IMAGE
FROM ${VERIFIED_IMAGE}
USER root
WORKDIR /verify
COPY . /verify
RUN cp /opt/hermes/install-stamp.json /verify/install-stamp.json && \
    /opt/hermes/.venv/bin/python -m pm.build_env --source /verify --out /verify/.venv --group dev --group test --extra messaging --sealed
RUN chown -R hermes:hermes /verify && mkdir -p /test-home && chown hermes:hermes /test-home
USER hermes
ENV HOME=/test-home HERMES_HOME=/test-home/hermes HERMES_PYTHON=/verify/.venv/bin/python CI=1
ENTRYPOINT ["bash", "-c"]
CMD ["exec bash scripts/run_tests.sh tests/gateway/test_api_server*.py tests/gateway/test_multiplex_api_server_routing.py tests/gateway/test_config.py tests/gateway/test_custom_provider_request_overrides.py tests/hermes_cli/test_container_boot.py tests/hermes_cli/test_gateway_external_supervisor.py -j 2"]
