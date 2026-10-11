ARG VERIFIED_IMAGE
FROM ${VERIFIED_IMAGE}
USER root
RUN groupmod -g 1000 hermes && usermod -u 1000 -g 1000 hermes && \
    chown hermes:hermes /opt/data /tmp/hermes-runtime
COPY --chmod=0444 scripts/standalone/gateway-trial-start.py /opt/hermes/gateway-trial-start.py
USER 1000:1000
WORKDIR /opt/data
ENV HOME=/opt/data HERMES_HOME=/opt/data API_SERVER_HOST=0.0.0.0 API_SERVER_PORT=7860
EXPOSE 7860
ENTRYPOINT ["/opt/hermes/.venv/bin/python", "/opt/hermes/gateway-trial-start.py"]
CMD []
