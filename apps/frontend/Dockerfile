FROM nginx:1.27-alpine
COPY app/index.html /usr/share/nginx/html/index.html
COPY app/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
