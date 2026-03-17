-- Scale embedded images to fit page width
function Image(el)
  el.attributes["width"] = "6in"
  return el
end
